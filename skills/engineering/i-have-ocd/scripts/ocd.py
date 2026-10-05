#!/usr/bin/env -S uv run --locked --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Machine-local i-have-ocd/1 reference backend. See references/backend-contract.md.

JSON is the default. Exit codes: 0 success, 1 unavailable, 2 invalid,
3 conflict, 4 forbidden. No command sends messages or accesses a network.
"""
import argparse
import difflib
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTRACT = "i-have-ocd/1"
SCHEMA_VERSION = 2
BUSY_SECONDS = 10.0
PAGE_SIZE = 100
JSON_TABLES = ("concerns", "decisions", "rulings", "handoffs", "reviews", "leases", "main_lines")
SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE concerns (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, concern_key TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(workspace_id, concern_key))",
    *(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)" for table in JSON_TABLES if table != "concerns"),
    "CREATE TABLE aliases (origin_store_id TEXT NOT NULL, legacy_id TEXT NOT NULL, concern_id TEXT NOT NULL REFERENCES concerns(id), PRIMARY KEY(origin_store_id, legacy_id))",
    "CREATE TABLE decision_links (concern_id TEXT NOT NULL REFERENCES concerns(id), decision_id TEXT NOT NULL REFERENCES decisions(id), PRIMARY KEY(concern_id, decision_id))",
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE requests (namespace TEXT NOT NULL, request_id TEXT NOT NULL, hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(namespace, request_id))",
)
READ_OPS = {"capabilities", "show", "find", "list", "history", "main.get", "review.show", "export", "import.plan", "migrate.plan"}
WRITE_OPS = {"intake", "link", "add_source", "main.set", "lease.acquire", "lease.renew", "lease.release",
             "route.offer", "route.accept", "route.reject", "route.cancel", "route.receipt", "decision.propose",
             "decision.link", "decision.answer", "decision.later", "decision.reopen", "decision.withdraw",
             "review.start", "review.present", "review.finish", "urgent", "close", "merge", "import.apply", "migrate.apply", "render"}
EXPECTED = {"intake": (), "link": (), "main.set": ("main",), "add_source": ("item",),
            "lease.acquire": ("item",), "lease.renew": ("item",), "lease.release": ("item",),
            "route.offer": ("item",), "route.accept": ("handoff",), "route.reject": ("handoff",),
            "route.cancel": ("handoff",), "route.receipt": ("handoff",), "decision.propose": ("item",),
            "decision.link": ("item", "decision"), "decision.answer": ("decision",), "decision.later": ("decision",),
            "decision.reopen": ("decision",), "decision.withdraw": ("decision",), "review.start": (),
            "review.present": ("review",), "review.finish": ("review",), "urgent": ("item",),
            "close": ("item",), "merge": ("from_item", "to_item"), "import.apply": ("revision",), "migrate.apply": ("revision",), "render": ()}


class Failure(Exception):
    code, kind = 1, "UNAVAILABLE"

    def __init__(self, reason, **state):
        super().__init__(reason)
        self.reason, self.state = reason, state


class Unavailable(Failure):
    pass


class Invalid(Failure):
    code, kind = 2, "INVALID"


class Conflict(Failure):
    code, kind = 3, "CONFLICT"


class Forbidden(Failure):
    code, kind = 4, "FORBIDDEN"


def utcnow():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.isoformat()


def new_id():
    return str(uuid.uuid4())


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise Invalid("MISSING_" + field.upper())
    return value.strip()


def require(data, *fields):
    if not isinstance(data, dict):
        raise Invalid("EXPECTED_OBJECT")
    for field in fields:
        text(data.get(field), field)


def key_parts(key):
    if not isinstance(key, dict) or set(key) != {"object", "consequence", "occurrence"}:
        raise Invalid("INVALID_CONCERN_KEY")
    return {k: unicodedata.normalize("NFC", text(v, k)) for k, v in key.items()}


def concern_key(key):
    return digest(key_parts(key))


def sources(data):
    values = data.get("sources")
    if not isinstance(values, list) or not values:
        raise Invalid("MISSING_SOURCES")
    for value in values:
        require(value, "ref", "evidence")
    return values


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k not in {"token", "claim_token", "token_hash"}}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def project_name(explicit=None):
    name = explicit or os.environ.get("I_HAVE_OCD_PROJECT")
    if not name:
        try:
            result = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=10)
            common = (Path.cwd() / result.stdout.strip()).resolve() if result.returncode == 0 else None
            name = common.parent.name if common and common.name == ".git" else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    name = name or Path.cwd().name
    if name.startswith(".") or "/" in name or "\\" in name:
        raise Invalid("INVALID_PROJECT")
    return name


def backend_guard(binding="local"):
    if os.environ.get("I_HAVE_OCD_BACKEND", binding) != "local" or binding != "local":
        raise Unavailable("SHARED_BACKEND_UNSUPPORTED")


def role_guard(actor, role, write):
    if write and (role == "child" or os.environ.get("I_HAVE_OCD_CHILD") == "1" or actor.get("parent_id") is not None):
        raise Forbidden("RETURN_TO_PARENT")
    if write and role != "parent":
        raise Forbidden("UNKNOWN_ROLE")


class Store:
    def __init__(self, project, actor_id="", by="", home=None):
        self.project, self.actor_id, self.by = project, actor_id, by
        home = Path(home or os.environ.get("I_HAVE_OCD_HOME") or "~/.local/state/i-have-ocd").expanduser().resolve()
        self.path, self.view = home / f"{project}.db", home / f"{project}.md"
        self.db = None

    def create(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{self.project}-", suffix=".new", dir=self.path.parent)
        os.close(fd)
        fresh = Path(name)
        db = sqlite3.connect(fresh)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            self.initialize(db)
            db.commit()
        finally:
            db.close()
        try:
            os.link(fresh, self.path)
        except FileExistsError:
            pass
        finally:
            fresh.unlink(missing_ok=True)

    @staticmethod
    def initialize(db, store_id=None, workspace_id=None, revision=0):
        for statement in SCHEMA:
            db.execute(statement)
        store_id = store_id or new_id()
        meta = {"store_id": store_id, "workspace_id": workspace_id or new_id(), "backend_id": store_id,
                "binding": "local", "cutover_epoch": "1", "revision": str(revision), "schema_version": "2"}
        db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        db.execute("PRAGMA user_version=2")

    def open(self, write=False, create=False):
        backend_guard()
        if not self.path.exists():
            if not create:
                raise Unavailable("STORE_MISSING")
            if self.view.exists():
                raise Unavailable("MIGRATION_REQUIRED")
            self.create()
        self.db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True,
                                  timeout=BUSY_SECONDS, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        schema = self.db.execute("PRAGMA user_version").fetchone()[0]
        if schema != 2:
            raise Unavailable("MIGRATION_REQUIRED" if schema == 1 else "SCHEMA_UNSUPPORTED")
        backend_guard(self.meta("binding"))
        return self

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def transaction(self, write=True):
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def commit(self):
        self.db.execute("COMMIT")

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def revision(self):
        return int(self.meta("revision"))

    def get(self, table, identity, required=True):
        row = self.db.execute(f"SELECT data FROM {table} WHERE id=?", (identity,)).fetchone()
        if row:
            return json.loads(row[0])
        if required:
            raise Invalid("NOT_FOUND", current={"id": identity})
        return None

    def rows(self, table):
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def put(self, table, data):
        if table == "concerns":
            self.db.execute("INSERT INTO concerns VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (data["id"], self.meta("workspace_id"), data["concern_key"], canonical(data)))
        else:
            self.db.execute(f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (data["id"], canonical(data)))

    def change(self, table, data, **changes):
        data = {**data, **changes, "version": data["version"] + 1}
        self.put(table, data)
        return data

    @staticmethod
    def cas(data, expected):
        if data["version"] != expected:
            raise Conflict("STALE_VERSION", current=redact(data))
        return data

    def active(self, item):
        if item["lifecycle"] != "active":
            raise Conflict("ITEM_NOT_ACTIVE", current=self.public(item))

    def responsible(self, item):
        if item["responsible_actor"] != self.actor_id:
            raise Forbidden("NOT_RESPONSIBLE_ACTOR")

    def linked_decisions(self, identity):
        return [self.get("decisions", row[0]) for row in self.db.execute(
            "SELECT decision_id FROM decision_links WHERE concern_id=? ORDER BY decision_id", (identity,))]

    def public(self, item, include_delivery=False):
        decisions = self.linked_decisions(item["id"])
        states = {d["state"] for d in decisions}
        attention = next((state for state in ("owner_pending", "later", "owner_decided") if state in states),
                         "routed" if item["routing"]["state"] == "accepted" or item.get("external_record") else "triage")
        result = {**item, "decisions": decisions, "needs_owner": "owner_pending" in states,
                  "attention_state": attention if item["lifecycle"] == "active" else item["lifecycle"]}
        if include_delivery:
            result["handoffs"] = [h for h in self.rows("handoffs") if h["concern_id"] == item["id"]]
        return redact(result)

    def resolve(self, identity, origin_store_id=None):
        item = self.get("concerns", identity, False)
        if not item:
            row = self.db.execute("SELECT concern_id FROM aliases WHERE origin_store_id=? AND legacy_id=?",
                                  (origin_store_id or self.meta("store_id"), identity)).fetchone()
            item = self.get("concerns", row[0]) if row else None
        if not item:
            raise Invalid("NOT_FOUND")
        seen = set()
        while item["lifecycle"] == "merged":
            if item["id"] in seen:
                raise Unavailable("ALIAS_CYCLE")
            seen.add(item["id"])
            item = self.get("concerns", item["merged_into"])
        return item

    def check_view(self, allow_missing=False):
        saved = self.meta("view_hash")
        if saved and (not self.view.exists() or digest(self.view.read_bytes()) != saved):
            if allow_missing and not self.view.exists():
                return
            raise Conflict("VIEW_DIVERGED")

    def event(self, op, data, expected=None):
        revision = self.revision() + 1
        identity = new_id()
        event = {"id": identity, "seq": revision, "op": op, "actor": {"id": self.actor_id, "by": self.by},
                 "at": iso(utcnow()), "expected": expected or {}, "detail": redact(data)}
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (revision, identity, canonical(event)))
        self.set_meta("revision", revision)
        return identity

    def envelope(self, result, request_id=None, events=(), replayed=False):
        result = dict(result)
        item, decision, handoff, review = (result.get(k) for k in ("item", "decision", "handoff", "review"))
        if item:
            result.update(item_id=item["id"], item_version=item["version"], attention_state=item["attention_state"],
                          needs_owner=item["needs_owner"])
        if decision:
            result.update(decision_id=decision["id"], decision_version=decision["version"])
        if handoff:
            result.update(handoff_id=handoff["id"], handoff_version=handoff["version"])
        if review:
            result.update({k: v for k, v in review.items() if k != "id"})
            result["review_id"] = review["id"]
        return {"ok": True, "contract": CONTRACT, "backend_id": self.meta("backend_id"),
                "store_id": self.meta("store_id"), "workspace_id": self.meta("workspace_id"),
                "revision": self.revision(), "request_id": request_id, "replayed": replayed,
                "result": result, "receipt": {"event_ids": list(events), "committed": True}}

    def execute(self, request, role="parent"):
        if not isinstance(request, dict) or request.get("contract") != CONTRACT:
            raise Invalid("CONTRACT_UNSUPPORTED")
        op = request.get("op")
        if op == "backend.bind":
            role_guard(request.get("actor", {}), role, True)
            raise Unavailable("SHARED_BACKEND_UNSUPPORTED")
        if op not in READ_OPS | WRITE_OPS:
            raise Invalid("UNKNOWN_OP")
        actor = request.get("actor", {})
        if not isinstance(actor, dict):
            raise Invalid("INVALID_ACTOR")
        write = op in WRITE_OPS
        role_guard(actor, role, write)
        if write:
            self.actor_id = text(actor.get("id"), "actor_id")
            self.by = actor.get("by", self.by)
            text(request.get("request_id"), "request_id")
        elif actor.get("id"):
            self.actor_id = actor["id"]
        if op.startswith("migrate."):
            data, expected = request.get("input", {}), request.get("expected", {})
            if not isinstance(data, dict) or not isinstance(data.get("mapping"), dict):
                raise Invalid("MISSING_MIGRATION_MAPPING")
            if not isinstance(expected, dict) or set(expected) != ({"revision"} if write else set()) or any(
                type(v) is not int or v < 0 for v in expected.values()
            ):
                raise Invalid("INVALID_EXPECTED_VERSIONS")
            if request.get("workspace_id") is not None and request["workspace_id"] != data["mapping"].get("workspace_id"):
                raise Forbidden("WORKSPACE_MISMATCH")
            return migrate(self, "apply" if write else "plan", data.get("source"), data["mapping"],
                           expected.get("revision"), request.get("request_id"), role)
        if op == "import.plan" and not self.path.exists():
            backend_guard()
            snapshot = validate_snapshot(request.get("input", {}))
            return {"ok": True, "contract": CONTRACT, "result": {"ready": True, "target_revision": 0,
                    "source_store_id": snapshot["store_id"], "source_revision": snapshot["revision"],
                    "digest": request["input"]["digest"]}, "receipt": {"committed": False, "event_ids": []}}
        self.open(write, create=op in {"intake", "link", "main.set", "import.apply"})
        with self.transaction(write):
            workspace = request.get("workspace_id")
            if workspace is not None and workspace != self.meta("workspace_id"):
                raise Forbidden("WORKSPACE_MISMATCH")
            self.check_view(allow_missing=op == "render")
            if not write:
                return self.envelope(self.read(op, request.get("input", {})))
            namespace = f"{self.meta('store_id')}:{self.meta('workspace_id')}:{CONTRACT}"
            request_hash = digest({k: request.get(k) for k in ("op", "workspace_id", "actor", "expected", "input", "lease", "leases")})
            known = self.db.execute("SELECT hash, result FROM requests WHERE namespace=? AND request_id=?",
                                    (namespace, request["request_id"])).fetchone()
            if known:
                if known["hash"] != request_hash:
                    raise Conflict("REQUEST_ID_REUSED")
                result = {**json.loads(known["result"]), "replayed": True}
            else:
                expected = request.get("expected", {})
                if not isinstance(expected, dict) or set(expected) != set(EXPECTED[op]) or any(
                    type(v) is not int or v < 0 for v in expected.values()
                ):
                    raise Invalid("INVALID_EXPECTED_VERSIONS")
                data = request.get("input", {})
                if not isinstance(data, dict):
                    raise Invalid("EXPECTED_OBJECT")
                result = self.write(op, data, expected, request)
                event_id = self.event(op, {"input": data, "result": result}, expected)
                result = self.envelope(result, request["request_id"], [event_id])
                namespace = f"{self.meta('store_id')}:{self.meta('workspace_id')}:{CONTRACT}"
                self.db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                                (namespace, request["request_id"], request_hash, canonical(result)))
        if op == "render":
            try:
                self.publish()
            except (Failure, OSError, sqlite3.Error) as error:
                raise Unavailable("VIEW_PUBLISH_FAILED", committed=True, detail=str(error))
        return result

    def lease_id(self, kind, identity):
        return f"{kind}:{identity}"

    def acquire(self, kind, identity, ttl=60):
        if type(ttl) is not int or not 0 < ttl <= 1440:
            raise Invalid("INVALID_LEASE_TTL")
        old = self.get("leases", self.lease_id(kind, identity), False)
        if old and old["expires_at"] > iso(utcnow()):
            raise Conflict("LEASE_HELD", current=redact(old))
        token = secrets.token_hex(32)
        lease = {"id": self.lease_id(kind, identity), "holder": self.actor_id, "token_hash": digest(token),
                 "fence": old["fence"] + 1 if old else 1, "expires_at": iso(utcnow() + timedelta(minutes=ttl))}
        self.put("leases", lease)
        return {**redact(lease), "token": token}

    def held(self, kind, identity, proof):
        lease = self.get("leases", self.lease_id(kind, identity), False)
        if not isinstance(proof, dict) or not lease or lease["holder"] != self.actor_id or \
                lease["token_hash"] != digest(proof.get("token", "")) or lease["fence"] != proof.get("fence") or \
                lease["expires_at"] <= iso(utcnow()):
            raise Conflict("STALE_LEASE")
        return lease

    def intake(self, data, linked=False, migration=False):
        if "needs_owner" in data:
            raise Invalid("NEEDS_OWNER_IS_DERIVED")
        require(data, "summary", "trigger", "owner", "responsible_actor")
        if not isinstance(data.get("consequence"), str) or not data["consequence"].strip():
            raise Invalid("NO_CONCRETE_CONSEQUENCE")
        sources(data)
        search = data.get("record_search", {})
        if not isinstance(search, dict) or not isinstance(search.get("checked"), list) or not search["checked"]:
            raise Invalid("MISSING_RECORD_SEARCH")
        require(search, "evidence_ref")
        if not linked and (search.get("result") != "none" or data.get("owning_record")):
            raise Invalid("USE_EXISTING_RECORD")
        record = data.get("owning_record")
        if linked:
            if search.get("result") != "found":
                raise Invalid("MISSING_OWNING_RECORD")
            require(record, "ref", "owner", "authority_ref")
            if record["owner"] != data["owner"]:
                raise Invalid("OWNING_RECORD_OWNER_MISMATCH")
        if not migration and data["responsible_actor"] != self.actor_id:
            raise Forbidden("INTAKE_ACTOR_MISMATCH")
        key = concern_key(data.get("key"))
        # A key stays indexed after closure and merge; wording and routing never alter it.
        row = self.db.execute("SELECT id FROM concerns WHERE workspace_id=? AND concern_key=?",
                              (self.meta("workspace_id"), key)).fetchone()
        if row:
            item = self.resolve(row[0])
            if linked and item["owning_record"] != record:
                raise Conflict("OWNING_RECORD_CONFLICT", current=self.public(item))
            return {"outcome": "existing", "item": self.public(item)}
        recurrence = data.get("recurrence_of")
        if recurrence:
            previous = self.get("concerns", recurrence)
            if previous["lifecycle"] != "closed" or previous.get("closure", {}).get("kind") != "resolved" or \
                    previous["key"]["occurrence"] == key_parts(data["key"])["occurrence"]:
                raise Invalid("NOT_A_NEW_RESOLVED_OCCURRENCE")
        identity = data.get("id", new_id()) if migration else new_id()
        item = {**data, "id": identity, "concern_key": key, "key": key_parts(data["key"]), "version": 1,
                "origin": {"store_id": self.meta("store_id"), "actor_id": self.actor_id},
                "owning_record": record or {"ref": f"ocd:{self.meta('store_id')}/{identity}", "owner": data["owner"],
                                             "authority_ref": search["evidence_ref"]},
                "external_record": linked, "routing": {"state": "owned"}, "lifecycle": "active",
                "urgency": data.get("urgency", {"level": "normal", "verification": "confirmed"})}
        self.put("concerns", item)
        return {"outcome": "linked" if linked else "created", "item": self.public(item)}

    def propose(self, item, data, migration=False):
        require(data, "question", "owner_only_reason", "authority_ref", "recommended", "inaction_consequence")
        options = data.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise Invalid("MISSING_OPTIONS")
        for option in options:
            require(option, "id", "label", "cost")
        ids = [o["id"] for o in options]
        if len(set(ids)) != len(ids) or data["recommended"] not in ids:
            raise Invalid("INVALID_OPTIONS")
        for previous in self.linked_decisions(item["id"]):
            if previous["question"] == data["question"] and previous["authority_ref"] == data["authority_ref"]:
                return previous
        identity = data.get("id", new_id()) if migration else new_id()
        decision = {**data, "id": identity, "owner_scope": data.get("owner_scope", "owner"),
                    "state": "owner_pending", "current_ruling_id": None, "reopen_condition": None,
                    "supersedes": data.get("supersedes"), "version": 1}
        if self.get("decisions", identity, False):
            raise Conflict("DECISION_ID_EXISTS")
        self.put("decisions", decision)
        self.db.execute("INSERT INTO decision_links VALUES (?, ?)", (item["id"], identity))
        return decision

    def ruling(self, decision, data, later=False):
        require(data, "answer_ref", "ruling_id")
        if later:
            condition = data.get("reopen_condition")
            self.condition(condition)
            answer = "later"
        else:
            answer = text(data.get("option_id"), "option_id")
            if answer not in {o["id"] for o in decision["options"]}:
                raise Invalid("UNKNOWN_OPTION")
        ruling = {"id": data["ruling_id"], "decision_id": decision["id"], "answer": answer,
                  "answer_ref": data["answer_ref"], "actor_id": self.actor_id, "at": iso(utcnow())}
        existing = self.get("rulings", ruling["id"], False)
        if existing:
            if any(existing[k] != ruling[k] for k in ("decision_id", "answer", "answer_ref")):
                raise Conflict("RULING_ID_REUSED")
        else:
            self.put("rulings", ruling)
        return self.change("decisions", decision, state="later" if later else "owner_decided",
                           current_ruling_id=ruling["id"], reopen_condition=data.get("reopen_condition") if later else None)

    @staticmethod
    def condition(condition):
        require(condition, "kind", "ref", "predicate")
        if condition["kind"] not in {"owner_request", "dependency_completed", "consequence_changed"}:
            raise Invalid("INVALID_REOPEN_CONDITION")

    def write(self, op, data, expected, request):
        proof = request.get("lease")
        if op in {"intake", "link"}:
            return self.intake(data, op == "link")
        if op == "main.set":
            identity = f"{self.meta('workspace_id')}:{self.actor_id}"
            current = self.get("main_lines", identity, False) or {"id": identity, "version": 0}
            self.cas(current, expected["main"])
            allowed = {"title", "done_when", "next_step", "paused_for", "set_by", "notes"}
            if not data or set(data) - allowed:
                raise Invalid("INVALID_MAIN_FIELDS")
            return {"main": self.change("main_lines", current, **data, actor_id=self.actor_id)}
        if op == "import.apply":
            return self.import_snapshot(data, expected["revision"])
        if op == "render":
            view = self.render_bytes()
            self.set_meta("view_text", view.decode())
            return {"view": self.view.name, "hash": digest(view)}
        if op.startswith("review."):
            return self.review_write(op, data, expected, proof)
        if op.startswith("decision.") and op not in {"decision.propose", "decision.link"}:
            decision = self.cas(self.get("decisions", data.get("decision_id")), expected["decision"])
            if op in {"decision.answer", "decision.later"}:
                if decision["state"] not in {"owner_pending", "later"}:
                    raise Conflict("DECISION_NOT_PENDING")
                decision = self.ruling(decision, data, op == "decision.later")
            elif op == "decision.reopen":
                if decision["state"] != "later":
                    raise Conflict("DECISION_NOT_LATER")
                condition = decision["reopen_condition"]
                require(data, "event_ref", "evidence_ref", "incremental_question")
                if data.get("event_kind") != condition["kind"] or data.get("condition_ref") != condition["ref"] or \
                        data.get("predicate") != condition["predicate"]:
                    raise Invalid("REOPEN_EVENT_MISMATCH")
                decision = self.change("decisions", decision, state="owner_pending", question=data["incremental_question"],
                                       reopen_event={k: data[k] for k in ("event_ref", "evidence_ref", "event_kind")})
            else:
                require(data, "evidence_ref", "reason")
                items = [self.get("concerns", row[0]) for row in self.db.execute(
                    "SELECT concern_id FROM decision_links WHERE decision_id=?", (decision["id"],))]
                for item in items:
                    self.responsible(item)
                if data.get("choice_no_longer_exists") is not True or data.get("basis") != "choice_removed":
                    raise Invalid("WITHDRAWAL_NOT_SUBSTANTIATED")
                decision = self.change("decisions", decision, state="withdrawn", withdrawal_ref=data["evidence_ref"],
                                       withdrawal_reason=data["reason"])
            return {"decision": decision}
        if op.startswith("route.") and op != "route.offer":
            handoff = self.cas(self.get("handoffs", data.get("handoff_id")), expected["handoff"])
            if handoff["state"] != "offered":
                raise Conflict("HANDOFF_NOT_OFFERED")
            item = self.get("concerns", handoff["concern_id"])
            self.active(item)
            if op == "route.accept":
                if self.actor_id != handoff["to_actor"]:
                    raise Forbidden("NOT_HANDOFF_TARGET")
                require(data, "owning_record")
                receipt = data.get("receipt")
                require(receipt, "ref", "responsibility")
                if receipt.get("kind") != "accepted" or data.get("offer_version") != handoff["offer_version"]:
                    raise Invalid("ACCEPTANCE_RECEIPT_REQUIRED")
                item = self.change("concerns", item, responsible_actor=self.actor_id, owner=handoff["to_owner"],
                                   owning_record={"ref": data["owning_record"], "owner": handoff["to_owner"],
                                                  "authority_ref": receipt["ref"]}, external_record=True,
                                   routing={"state": "accepted", "handoff_id": handoff["id"]})
                handoff = self.change("handoffs", handoff, state="accepted", acceptance_receipt=receipt)
                previous_lease = self.get("leases", self.lease_id("concern", item["id"]), False)
                if previous_lease:
                    self.put("leases", {**previous_lease, "token_hash": "fenced", "fence": previous_lease["fence"] + 1,
                                        "expires_at": iso(utcnow())})
            elif op == "route.receipt":
                if self.actor_id not in {handoff["from_actor"], handoff["to_actor"]}:
                    raise Forbidden("NOT_HANDOFF_PARTICIPANT")
                receipt = data.get("receipt")
                require(receipt, "kind", "ref")
                if receipt["kind"] not in {"queued", "delivered", "ack"}:
                    raise Invalid("INVALID_DELIVERY_RECEIPT")
                handoff = self.change("handoffs", handoff, delivery_receipts=handoff["delivery_receipts"] + [receipt])
            else:
                require(data, "reason")
                if op == "route.cancel":
                    self.responsible(item)
                    self.held("concern", item["id"], proof)
                elif self.actor_id != handoff["to_actor"]:
                    raise Forbidden("NOT_HANDOFF_TARGET")
                handoff = self.change("handoffs", handoff, state="cancelled" if op == "route.cancel" else "rejected",
                                      reason=data["reason"])
                item = self.change("concerns", item, routing={"state": "owned"})
            return {"handoff": handoff, "item": self.public(item), "delivery": "manual"}
        if op == "merge":
            return self.merge(data, expected, request.get("leases"))
        item = self.cas(self.get("concerns", data.get("item_id")), expected["item"])
        if op == "add_source":
            added = sources(data)
            combined = item["sources"] + [s for s in added if s not in item["sources"]]
            if combined != item["sources"]:
                item = self.change("concerns", item, sources=combined)
            return {"item": self.public(item)}
        self.active(item)
        if op.startswith("lease."):
            if op == "lease.acquire":
                lease = self.acquire("concern", item["id"], data.get("ttl_minutes", 60))
            else:
                lease = self.held("concern", item["id"], proof)
                ttl = data.get("ttl_minutes", 60)
                if type(ttl) is not int or not 0 < ttl <= 1440:
                    raise Invalid("INVALID_LEASE_TTL")
                lease["expires_at"] = iso(utcnow() + timedelta(minutes=ttl)) if op == "lease.renew" else iso(utcnow())
                self.put("leases", lease)
                lease = redact(lease)
            return {"item": self.public(item), "lease": lease}
        self.responsible(item)
        if op == "decision.propose":
            decision = self.propose(item, data)
            item = self.change("concerns", item)
            return {"item": self.public(item), "decision": decision}
        if op == "decision.link":
            decision = self.cas(self.get("decisions", data.get("decision_id")), expected["decision"])
            require(data, "evidence_ref")
            self.db.execute("INSERT OR IGNORE INTO decision_links VALUES (?, ?)", (item["id"], decision["id"]))
            item = self.change("concerns", item)
            return {"item": self.public(item), "decision": decision}
        if op == "urgent":
            if data.get("action") == "raise":
                require(data, "facts", "pending_verification", "consequence", "responder")
                urgency = {**item["urgency"], **data, "level": "urgent",
                           "first_notification": item["urgency"].get("first_notification")}
            elif data.get("action") == "notified":
                require(data, "notification_ref")
                if item["urgency"].get("level") != "urgent":
                    raise Invalid("URGENCY_NOT_RAISED")
                urgency = {**item["urgency"], "first_notification": item["urgency"].get("first_notification") or
                           {"ref": data["notification_ref"], "at": iso(utcnow())}}
            else:
                raise Invalid("INVALID_URGENT_ACTION")
            item = self.change("concerns", item, urgency=urgency)
        elif op == "route.offer":
            self.held("concern", item["id"], proof)
            target = data.get("target")
            require(target, "actor_id", "owner")
            require(data, "proposed_record", "authorization_ref", "instruction")
            if any(h["state"] == "offered" and h["concern_id"] == item["id"] for h in self.rows("handoffs")):
                raise Conflict("HANDOFF_ALREADY_OFFERED")
            handoff = {"id": new_id(), "concern_id": item["id"], "offer_version": 1, "version": 1,
                       "from_actor": self.actor_id, "to_actor": target["actor_id"], "to_owner": target["owner"],
                       "proposed_record": data["proposed_record"], "authorization_ref": data["authorization_ref"],
                       "instruction": data["instruction"], "state": "offered", "delivery_receipts": [], "acceptance_receipt": None}
            self.put("handoffs", handoff)
            item = self.change("concerns", item, routing={"state": "offered", "handoff_id": handoff["id"]})
            return {"item": self.public(item), "handoff": handoff, "delivery": "manual"}
        elif op == "close":
            self.held("concern", item["id"], proof)
            require(data, "kind", "ref", "evidence")
            if data["kind"] not in {"resolved", "not_applicable", "owner_dropped"}:
                raise Invalid("INVALID_CLOSURE_RECEIPT")
            if any(d["state"] in {"owner_pending", "later"} for d in self.linked_decisions(item["id"])):
                raise Conflict("UNANSWERED_DECISION")
            if any(h["state"] == "offered" and h["concern_id"] == item["id"] for h in self.rows("handoffs")):
                raise Conflict("HANDOFF_STILL_OFFERED")
            item = self.change("concerns", item, lifecycle="closed", closure={k: data[k] for k in ("kind", "ref", "evidence")})
        else:
            raise Invalid("UNKNOWN_OP")
        return {"item": self.public(item)}

    def merge(self, data, expected, proofs):
        source = self.cas(self.get("concerns", data.get("from_item")), expected["from_item"])
        target = self.cas(self.get("concerns", data.get("to_item")), expected["to_item"])
        if source["id"] == target["id"]:
            raise Invalid("SELF_MERGE")
        require(data, "evidence_ref", "reason")
        for item, side in ((source, "from_item"), (target, "to_item")):
            self.active(item)
            self.responsible(item)
            self.held("concern", item["id"], (proofs or {}).get(side))
            if item["routing"]["state"] == "offered":
                raise Conflict("HANDOFF_STILL_OFFERED")
        for decision in self.linked_decisions(source["id"]):
            self.db.execute("INSERT OR IGNORE INTO decision_links VALUES (?, ?)", (target["id"], decision["id"]))
        target = self.change("concerns", target, sources=target["sources"] + [s for s in source["sources"] if s not in target["sources"]])
        source = self.change("concerns", source, lifecycle="merged", merged_into=target["id"],
                             merge_evidence={"ref": data["evidence_ref"], "reason": data["reason"]})
        return {"from_item": self.public(source), "item": self.public(target)}

    def review_write(self, op, data, expected, proof):
        if op == "review.start":
            require(data, "owner_scope")
            trigger = data.get("trigger")
            require(trigger, "kind", "ref")
            if trigger["kind"] not in {"owner_request", "main_line_blocker"} or "limit" in data:
                raise Invalid("REVIEW_NEEDS_EXPLICIT_TRIGGER")
            active = next((r for r in self.rows("reviews") if r["owner_scope"] == data["owner_scope"] and r["state"] == "active"), None)
            if active:
                lease = self.get("leases", self.lease_id("review", active["id"]), False)
                result = {"review": self.review_public(active), "resumed": True}
                if not lease or lease["expires_at"] <= iso(utcnow()):
                    result["lease"] = self.acquire("review", active["id"])
                return result
            previous = [r for r in self.rows("reviews") if r["owner_scope"] == data["owner_scope"]]
            if previous and trigger["ref"] == previous[-1]["trigger"]["ref"]:
                raise Conflict("REVIEW_NEEDS_NEW_TRIGGER")
            eligible = [d for d in self.rows("decisions") if d["state"] == "owner_pending" and d["owner_scope"] == data["owner_scope"]]
            # Every pending decision in the scope, across lanes: the human answers them in one review.
            review = {"id": new_id(), "version": 1, "owner_scope": data["owner_scope"], "trigger": trigger,
                      "snapshot_revision": self.revision(), "snapshot": [{"id": d["id"], "version": d["version"]} for d in eligible],
                      "presented_ids": [], "answer_refs": [], "state": "active"}
            self.put("reviews", review)
            return {"review": self.review_public(review), "lease": self.acquire("review", review["id"]), "resumed": False}
        review = self.cas(self.get("reviews", data.get("review_id")), expected["review"])
        if review["state"] != "active":
            raise Conflict("REVIEW_NOT_ACTIVE")
        self.held("review", review["id"], proof)
        if op == "review.present":
            valid = [d["id"] for d in self.review_public(review)["decisions"]]
            proposed = data.get("decision_ids", valid)
            if not isinstance(proposed, list) or len(set(proposed)) != len(proposed) or set(proposed) - set(valid):
                raise Conflict("REVIEW_SNAPSHOT_CHANGED")
            cumulative = list(dict.fromkeys(review["presented_ids"] + proposed))
            require(data, "presentation_ref")
            review = self.change("reviews", review, presented_ids=cumulative, presentation_ref=data["presentation_ref"])
        else:
            require(data, "finish_ref")
            refs = [self.get("rulings", d["current_ruling_id"])["answer_ref"] for d in self.rows("decisions")
                    if d["id"] in {s["id"] for s in review["snapshot"]} and d.get("current_ruling_id")]
            review = self.change("reviews", review, state="finished", finish_ref=data["finish_ref"], answer_refs=refs)
            lease = self.get("leases", self.lease_id("review", review["id"]))
            lease["expires_at"] = iso(utcnow())
            self.put("leases", lease)
        return {"review": self.review_public(review)}

    def review_public(self, review):
        decisions = []
        for snapshot in review["snapshot"]:
            decision = self.get("decisions", snapshot["id"])
            if decision["state"] == "owner_pending" and decision["version"] == snapshot["version"]:
                links = [r[0] for r in self.db.execute("SELECT concern_id FROM decision_links WHERE decision_id=?", (decision["id"],))]
                decisions.append({**decision, "concern_ids": links})
        return {**review, "decisions": decisions,
                "presenter_lease": redact(self.get("leases", self.lease_id("review", review["id"]), False))}

    def page(self, values, data):
        cursor = data.get("cursor")
        offset, watermark = 0, self.revision()
        if cursor:
            try:
                value = json.loads(cursor)
                offset, watermark = value["offset"], value["revision"]
                if type(offset) is not int or offset < 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise Invalid("INVALID_CURSOR")
            if watermark != self.revision():
                raise Conflict("CURSOR_REVISION_CHANGED")
        part = values[offset:offset + PAGE_SIZE]
        next_cursor = canonical({"offset": offset + PAGE_SIZE, "revision": watermark}) if offset + PAGE_SIZE < len(values) else None
        return part, next_cursor

    def read(self, op, data):
        if not isinstance(data, dict):
            raise Invalid("EXPECTED_OBJECT")
        if op == "capabilities":
            return {"contract": CONTRACT, "schema": 2, "delivery": "manual", "backend": "local",
                    "backend_id": self.meta("backend_id"), "store_id": self.meta("store_id"), "workspace_id": self.meta("workspace_id"),
                    "ops": sorted(READ_OPS | WRITE_OPS),
                    "roles": {"parent": "read/write", "child": "read/return_to_parent"}}
        if op in {"show", "find"}:
            if op == "find":
                key = concern_key(data["key"]) if isinstance(data.get("key"), dict) else text(data.get("key"), "key")
                row = self.db.execute("SELECT id FROM concerns WHERE workspace_id=? AND concern_key=?",
                                      (self.meta("workspace_id"), key)).fetchone()
                if not row:
                    return {"item": None}
                identity = row[0]
            else:
                identity = data.get("item_id")
            item = self.resolve(identity, data.get("origin_store_id"))
            result = {"item": self.public(item, data.get("include_delivery", False))}
            if data.get("history"):
                result["history"] = self.history()
            return result
        if op == "list":
            items = [self.public(i) for i in self.rows("concerns")]
            owner, state = data.get("owner"), data.get("state", "active")
            items = [i for i in items if (not owner or i["owner"] == owner) and
                     (state == "all" or state in (i["lifecycle"], i["attention_state"], i["routing"]["state"]))]
            part, cursor = self.page(items, data)
            result = {"items": part, "cursor": cursor}
            if data.get("include_counts"):
                result["counts"] = {"work": len(items), "owner_pending": sum(i["needs_owner"] for i in items),
                                    "later": sum(i["attention_state"] == "later" for i in items)}
            return result
        if op == "main.get":
            text(self.actor_id, "actor_id")
            main = self.get("main_lines", f"{self.meta('workspace_id')}:{self.actor_id}", False)
            return {"main": main, "version": main["version"] if main else 0}
        if op == "review.show":
            return {"review": self.review_public(self.get("reviews", data.get("review_id")))}
        if op == "history":
            values = [e for e in self.history() if e["seq"] > data.get("after_event", 0)]
            part, cursor = self.page(values, data)
            return {"events": part, "cursor": cursor}
        if op == "export":
            return self.export_snapshot(data)
        if op == "import.plan":
            snapshot = validate_snapshot(data)
            return {"source_store_id": snapshot["store_id"], "source_revision": snapshot["revision"],
                    "target_revision": self.revision(), "digest": data["digest"], "ready": self.revision() == 0}
        raise Invalid("UNKNOWN_OP")

    def history(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM events ORDER BY seq")]

    def render_bytes(self):
        lines = ["<!-- GENERATED by i-have-ocd. Read-only; edits are never imported. -->",
                 f"# {self.project} (machine-local)", "", "## Main lines"]
        for main in self.rows("main_lines"):
            lines.append(f"- {main['actor_id']}: {main.get('title', '')} · {main.get('next_step', '')}")
        lines.extend(["", "## Concerns"])
        for item in self.rows("concerns"):
            public = self.public(item)
            lines.append(f"- {item['id']} · {item['summary']} · {item['owner']} · {public['attention_state']}")
        return ("\n".join(lines) + "\n").encode()

    def publish(self):
        # A lost render can be explicitly retried. Reads never repair or absorb files.
        with self.transaction():
            self.check_view(allow_missing=True)
            data = self.meta("view_text").encode()
            fd, name = tempfile.mkstemp(prefix=self.view.name, suffix=".tmp", dir=self.view.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.view)
                self.set_meta("view_hash", digest(data))
            finally:
                Path(name).unlink(missing_ok=True)

    def export_snapshot(self, data):
        if not data.get("include_history") or data.get("after_event", 0) != 0:
            raise Invalid("FULL_HISTORY_REQUIRED")
        tables = {table: [redact(dict(row)) for row in self.db.execute(f"SELECT * FROM {table}")] for table in JSON_TABLES}
        for table in JSON_TABLES:
            for row in tables[table]:
                row["data"] = canonical(redact(json.loads(row["data"])))
        for table in ("aliases", "decision_links", "events", "requests"):
            tables[table] = [dict(row) for row in self.db.execute(f"SELECT * FROM {table}")]
        for row in tables["requests"]:
            row["result"] = canonical(redact(json.loads(row["result"])))
        legacy = {}
        for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'legacy_%'"):
            legacy[row[0]] = [redact(dict(r)) for r in self.db.execute(f'SELECT * FROM "{row[0]}"')]
        legacy = redact_json_strings(legacy)
        snapshot = {"contract": CONTRACT, "schema": 2, "store_id": self.meta("store_id"),
                    "workspace_id": self.meta("workspace_id"), "revision": self.revision(), "tables": tables,
                    "legacy": legacy, "cutover_epoch": self.meta("cutover_epoch")}
        return {"snapshot": snapshot, "digest": digest(snapshot)}

    def import_snapshot(self, data, expected_revision):
        snapshot = validate_snapshot(data)
        if self.revision() != expected_revision:
            raise Conflict("STALE_REVISION", current={"revision": self.revision()})
        if self.revision() != 0 or any(self.rows(table) for table in JSON_TABLES):
            raise Conflict("IMPORT_TARGET_NOT_EMPTY")
        for table, rows in snapshot["tables"].items():
            for row in rows:
                row = dict(row)
                if table == "leases":
                    lease = json.loads(row["data"])
                    lease.update(token_hash="fenced", expires_at=iso(utcnow()), fence=lease["fence"] + 1)
                    row["data"] = canonical(lease)
                if table == "reviews":
                    # Preserve the fixed snapshot; the next explicit start reacquires its presenter lease.
                    row["data"] = canonical(json.loads(row["data"]))
                columns = list(row)
                self.db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row.values()))
        for name, rows in snapshot.get("legacy", {}).items():
            self.db.execute(f'CREATE TABLE "{name}" (data TEXT NOT NULL)')
            self.db.executemany(f'INSERT INTO "{name}" VALUES (?)', [(canonical(row),) for row in rows])
        for key in ("store_id", "workspace_id", "revision", "cutover_epoch"):
            self.set_meta(key, snapshot[key])
        self.set_meta("backend_id", snapshot["store_id"])
        self.set_meta("cutover_epoch", str(int(snapshot["cutover_epoch"]) + 1))
        return {"imported": True, "source_digest": data["digest"], "leases_fenced": True}


def redact_json_strings(value):
    if isinstance(value, dict):
        return {k: redact_json_strings(v) for k, v in redact(value).items()}
    if isinstance(value, list):
        return [redact_json_strings(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, (dict, list)):
            return canonical(redact(parsed))
    return value


def validate_snapshot(data):
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict) or digest(snapshot) != data.get("digest"):
        raise Invalid("SNAPSHOT_DIGEST_MISMATCH")
    if snapshot.get("contract") != CONTRACT or snapshot.get("schema") != 2:
        raise Invalid("SNAPSHOT_INCOMPATIBLE")
    tables = snapshot.get("tables", {})
    if set(tables) != set(JSON_TABLES) | {"aliases", "decision_links", "events", "requests"}:
        raise Invalid("SNAPSHOT_TABLES_INCOMPLETE")
    allowed = {"concerns": {"id", "workspace_id", "concern_key", "data"},
               **{t: {"id", "data"} for t in JSON_TABLES if t != "concerns"},
               "aliases": {"origin_store_id", "legacy_id", "concern_id"},
               "decision_links": {"concern_id", "decision_id"}, "events": {"seq", "id", "data"},
               "requests": {"namespace", "request_id", "hash", "result"}}
    for table, rows in tables.items():
        if not isinstance(rows, list) or any(not isinstance(r, dict) or set(r) != allowed[table] for r in rows):
            raise Invalid("INVALID_SNAPSHOT_ROWS")
    require(snapshot, "store_id", "workspace_id", "cutover_epoch")
    if type(snapshot.get("revision")) is not int or snapshot["revision"] < 0:
        raise Invalid("INVALID_SNAPSHOT_REVISION")
    decoded = {}
    for table in JSON_TABLES:
        decoded[table] = {}
        for row in tables[table]:
            obj = json.loads(row["data"])
            require(obj, "id")
            if obj["id"] != row["id"] or obj["id"] in decoded[table]:
                raise Invalid("DUPLICATE_OR_MISMATCHED_ID")
            if table not in {"leases", "rulings"} and (type(obj.get("version")) is not int or obj["version"] < 1):
                raise Invalid("INVALID_OBJECT_VERSION")
            decoded[table][obj["id"]] = obj
    ids = {t: {r["id"] for r in tables[t]} for t in JSON_TABLES}
    if any(r["concern_id"] not in ids["concerns"] or r["decision_id"] not in ids["decisions"] for r in tables["decision_links"]):
        raise Invalid("DANGLING_DECISION_LINK")
    linked = {r["decision_id"] for r in tables["decision_links"]}
    if ids["decisions"] - linked:
        raise Invalid("UNLINKED_DECISION")
    keys = set()
    for row in tables["concerns"]:
        item = decoded["concerns"][row["id"]]
        require(item, "summary", "trigger", "consequence", "owner", "responsible_actor")
        sources(item)
        require(item.get("owning_record"), "ref", "owner", "authority_ref")
        if row["workspace_id"] != snapshot["workspace_id"] or row["concern_key"] != concern_key(item.get("key")) or \
                row["concern_key"] != item.get("concern_key") or row["concern_key"] in keys:
            raise Invalid("INVALID_SNAPSHOT_CONCERN_KEY")
        keys.add(row["concern_key"])
        if item.get("lifecycle") not in {"active", "closed", "merged"} or item.get("routing", {}).get("state") not in {"owned", "offered", "accepted"}:
            raise Invalid("INVALID_CONCERN_STATE")
        linked_states = {decoded["decisions"][link["decision_id"]]["state"] for link in tables["decision_links"] if link["concern_id"] == item["id"]}
        if item["lifecycle"] == "closed":
            closure = item.get("closure")
            require(closure, "kind", "ref", "evidence")
            if closure["kind"] not in {"resolved", "not_applicable", "owner_dropped"} or linked_states & {"owner_pending", "later"}:
                raise Invalid("CLOSED_WITH_UNANSWERED_DECISION")
        if item["lifecycle"] == "merged":
            target = decoded["concerns"].get(item.get("merged_into"))
            if not target or target["id"] == item["id"]:
                raise Invalid("INVALID_MERGED_TARGET")
            source_links = {r["decision_id"] for r in tables["decision_links"] if r["concern_id"] == item["id"]}
            target_links = {r["decision_id"] for r in tables["decision_links"] if r["concern_id"] == target["id"]}
            if source_links - target_links:
                raise Invalid("MERGE_LOST_DECISION")
    for alias in tables["aliases"]:
        if alias["concern_id"] not in ids["concerns"]:
            raise Invalid("DANGLING_ALIAS")
    for decision in decoded["decisions"].values():
        require(decision, "question", "owner_only_reason", "authority_ref", "recommended", "inaction_consequence")
        if decision.get("state") not in {"owner_pending", "owner_decided", "later", "withdrawn"}:
            raise Invalid("INVALID_DECISION_STATE")
        ruling = decoded["rulings"].get(decision.get("current_ruling_id"))
        if decision["state"] in {"owner_decided", "later"} and (not ruling or ruling.get("decision_id") != decision["id"]):
            raise Invalid("MISSING_CURRENT_RULING")
        if decision["state"] == "later":
            Store.condition(decision.get("reopen_condition"))
        if decision["state"] == "withdrawn":
            require(decision, "withdrawal_ref", "withdrawal_reason")
    for ruling in decoded["rulings"].values():
        require(ruling, "decision_id", "answer", "answer_ref", "actor_id", "at")
        if ruling["decision_id"] not in ids["decisions"]:
            raise Invalid("DANGLING_RULING")
    offered = set()
    for handoff in decoded["handoffs"].values():
        item = decoded["concerns"].get(handoff.get("concern_id"))
        if not item:
            raise Invalid("DANGLING_HANDOFF")
        if handoff["state"] == "offered":
            if item["id"] in offered:
                raise Invalid("MULTIPLE_ACTIVE_OFFERS")
            offered.add(item["id"])
        if handoff["state"] == "accepted":
            receipt = handoff.get("acceptance_receipt")
            require(receipt, "ref", "responsibility")
            if receipt.get("kind") != "accepted":
                raise Invalid("UNVERIFIED_ACCEPTANCE")
    active_scopes = set()
    for review in decoded["reviews"].values():
        snapshot_ids = [d["id"] for d in review["snapshot"]]
        if len(set(snapshot_ids)) != len(snapshot_ids) or set(snapshot_ids) - ids["decisions"] or \
                set(review["presented_ids"]) - set(snapshot_ids):
            raise Invalid("INVALID_REVIEW_SNAPSHOT")
        if review["state"] == "active":
            if review["owner_scope"] in active_scopes:
                raise Invalid("MULTIPLE_ACTIVE_REVIEWS")
            active_scopes.add(review["owner_scope"])
    for name in snapshot.get("legacy", {}):
        if name not in {"legacy_items", "legacy_events", "legacy_requests", "legacy_meta", "legacy_main_line", "legacy_markdown"}:
            raise Invalid("INVALID_LEGACY_TABLE")
    return snapshot


def legacy_snapshot(db, origin_store_id=None):
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise Unavailable("MIGRATION_SOURCE_NOT_FORMAT_1")
    result = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")] for table in ("items", "events", "requests", "meta", "main_line")}
    result["revision"] = max((e["seq"] for e in result["events"]), default=0)
    result["origin_store_id"] = next((r["value"] for r in result["meta"] if r["key"] == "store_id"),
                                     origin_store_id or new_id())
    return result


def read_backup(path, destination=None):
    if not path.exists():
        raise Unavailable("MIGRATION_SOURCE_MISSING")
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=BUSY_SECONDS)
    backup = sqlite3.connect(destination or ":memory:")
    backup.row_factory = sqlite3.Row
    try:
        source.backup(backup)
    finally:
        source.close()
    return backup


def migration_plan(snapshot, mapping, view):
    rows = mapping.get("items", [])
    indexed = {row.get("legacy_id"): row for row in rows}
    gaps = []
    if len(indexed) != len(rows) or set(indexed) != {f"P{i['id']}" for i in snapshot["items"]}:
        gaps.append("EVERY_LEGACY_ID_REQUIRES_ONE_MAPPING")
    for old in snapshot["items"]:
        row = indexed.get(f"P{old['id']}", {})
        if row.get("old_version") != old["version"] or row.get("raw_hash") != digest(old) or row.get("origin_store_id") != snapshot["origin_store_id"]:
            gaps.append(f"P{old['id']}: SOURCE_CHANGED")
        if row.get("uncertainty") != []:
            gaps.append(f"P{old['id']}: UNRESOLVED_UNCERTAINTY")
        if row.get("classification") not in {"engineering", "route_only", "owner_pending", "owner_decided", "later", "not_applicable", "resolved", "owner_dropped"}:
            gaps.append(f"P{old['id']}: INVALID_CLASSIFICATION")
        if not row.get("concern"):
            gaps.append(f"P{old['id']}: MISSING_CONCERN_EVIDENCE")
        if not isinstance(row.get("decision_ids"), list) or row["decision_ids"] != [d.get("id") for d in row.get("decisions", [])]:
            gaps.append(f"P{old['id']}: DECISION_MAPPING_INCOMPLETE")
    saved_hash = next((r["value"] for r in snapshot["meta"] if r["key"] == "view_hash"), None)
    current_hash = digest(view) if view is not None else None
    view_diverged = saved_hash != current_hash
    if view_diverged and (mapping.get("view_resolution", {}).get("hash") != current_hash or not mapping.get("view_resolution", {}).get("evidence_ref")):
        gaps.append("UNABSORBED_MARKDOWN_REQUIRES_EXPLICIT_MAPPING")
    diff = []
    if view_diverged:
        old_lines = json.loads(next((r["value"] for r in snapshot["meta"] if r["key"] == "view_items"), "{}"))
        diff = list(difflib.unified_diff([str(v) for v in old_lines.values()], (view or b"").decode(errors="replace").splitlines(), lineterm=""))
    if not gaps:
        probe = Store("mapping-check", "migration-validator")
        probe.db = sqlite3.connect(":memory:")
        probe.db.row_factory = sqlite3.Row
        try:
            Store.initialize(probe.db, snapshot["origin_store_id"], mapping.get("workspace_id"))
            for row in rows:
                migrate_item(probe, row)
            for row in mapping.get("additional_concerns", []):
                migrate_additional(probe, row)
        except (Failure, sqlite3.Error, KeyError, TypeError, ValueError) as error:
            gaps.append(str(error))
        finally:
            probe.close()
    return {"ready": not gaps, "gaps": gaps, "revision": snapshot["revision"], "origin_store_id": snapshot["origin_store_id"],
            "source_digest": digest(snapshot), "items": [{"legacy_id": f"P{i['id']}", "old_version": i["version"],
            "raw_hash": digest(i), "raw": redact(i)} for i in snapshot["items"]], "markdown_diff": diff, "view_hash": current_hash}


def migrate(store, action, source, mapping, expected_revision, request_id, role):
    backend_guard()
    role_guard({"id": store.actor_id}, role, action == "apply")
    source = Path(source or store.path).expanduser().resolve()
    backup = read_backup(source)
    try:
        if backup.execute("PRAGMA user_version").fetchone()[0] == 2 and action == "apply":
            namespace = ":".join(backup.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()[0]
                                 for k in ("store_id", "workspace_id")) + ":" + CONTRACT
            known = backup.execute("SELECT hash, result FROM requests WHERE namespace=? AND request_id=?",
                                   (namespace, request_id)).fetchone()
            migration_hash = digest({"mapping": mapping, "expected_revision": expected_revision, "actor_id": store.actor_id})
            if not known or known["hash"] != migration_hash:
                raise Conflict("MIGRATION_ALREADY_APPLIED")
            return {**json.loads(known["result"]), "replayed": True}
        declared_origins = {r.get("origin_store_id") for r in mapping.get("items", [])}
        if len(declared_origins) > 1:
            raise Invalid("MIXED_SOURCE_STORES")
        proposed_origin = next(iter(declared_origins), mapping.get("origin_store_id"))
        snapshot = legacy_snapshot(backup, proposed_origin)
    finally:
        backup.close()
    view_path = source.with_suffix(".md")
    view = view_path.read_bytes() if view_path.exists() else None
    plan = migration_plan(snapshot, mapping, view)
    if action == "plan":
        return {"ok": True, "contract": CONTRACT, "result": plan}
    text(request_id, "request_id")
    text(store.actor_id, "actor_id")
    if source != store.path:
        raise Invalid("MIGRATE_SOURCE_MUST_BE_TARGET")
    # Refuse a cutover that raced the read-only snapshot.
    check = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    schema = check.execute("PRAGMA user_version").fetchone()[0]
    check.close()
    if schema != 1:
        raise Conflict("MIGRATION_ALREADY_APPLIED")
    if not plan["ready"]:
        raise Invalid("MAPPING_INCOMPLETE", current=plan)
    if expected_revision != snapshot["revision"]:
        raise Conflict("STALE_REVISION", current={"revision": snapshot["revision"]})
    backup_path = source.with_name(source.name + f".pre-migration-{digest(snapshot)[:16]}")
    durable_backup = read_backup(source, str(backup_path))
    durable_backup.close()
    store.db = sqlite3.connect(source.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=BUSY_SECONDS)
    store.db.row_factory = sqlite3.Row
    with store.transaction():
        if digest(legacy_snapshot(store.db, snapshot["origin_store_id"])) != digest(snapshot) or (view_path.read_bytes() if view_path.exists() else None) != view:
            raise Conflict("MIGRATION_SOURCE_CHANGED")
        for table in ("items", "events", "requests", "meta", "main_line"):
            store.db.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
        Store.initialize(store.db, snapshot["origin_store_id"], mapping.get("workspace_id"), snapshot["revision"])
        store.set_meta("cutover_epoch", "2")
        store.db.execute("CREATE TABLE legacy_markdown (data TEXT NOT NULL)")
        store.db.execute("INSERT INTO legacy_markdown VALUES (?)", (canonical({"text": (view or b"").decode(errors="replace"), "hash": plan["view_hash"]}),))
        for row in mapping["items"]:
            migrate_item(store, row)
        for row in mapping.get("additional_concerns", []):
            migrate_additional(store, row)
        # Legacy raw tables and backup retain provenance; none of their claims grants a new lease.
        if view is not None:
            store.set_meta("view_hash", digest(view))
        result = {"migrated": True, "backup": backup_path.name, "legacy_main_preserved": True, "leases_fenced": True}
        event = store.event("migrate.apply", {"mapping": mapping, "source_digest": digest(snapshot), "result": result})
        result = store.envelope(result, request_id, [event])
        namespace = f"{store.meta('store_id')}:{store.meta('workspace_id')}:{CONTRACT}"
        store.db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                         (namespace, request_id, digest({"mapping": mapping, "expected_revision": expected_revision,
                                                       "actor_id": store.actor_id}), canonical(result)))
    return result


def migrate_item(store, row):
    require(row, "concern_id", "owner", "responsible_actor", "owning_record", "disposition")
    if not isinstance(row.get("receipt_refs"), list) or not row["receipt_refs"]:
        raise Invalid("MISSING_MAPPING_RECEIPT_REFS")
    data = {**row["concern"], "id": row["concern_id"], "owner": row["owner"], "responsible_actor": row["responsible_actor"]}
    item = store.intake(data, data.get("record_search", {}).get("result") == "found", migration=True)["item"]
    if item["id"] != row["concern_id"] or item["owning_record"]["ref"] != row["owning_record"]:
        raise Invalid("MAPPING_RECORD_MISMATCH")
    store.db.execute("INSERT INTO aliases VALUES (?, ?, ?)", (row["origin_store_id"], row["legacy_id"], item["id"]))
    classification = row["classification"]
    if classification == "route_only" and row.get("decisions"):
        raise Invalid("ROUTE_IS_NOT_OWNER_ANSWER")
    for proposed in row.get("decisions", []):
        existing = store.get("decisions", proposed["id"], False)
        if existing:
            if any(existing.get(k) != proposed.get(k) for k in
                   ("question", "authority_ref", "options", "recommended", "owner_only_reason", "inaction_consequence")):
                raise Invalid("CANONICAL_DECISION_MISMATCH")
            if proposed.get("state", "owner_pending") != existing["state"]:
                raise Invalid("CANONICAL_DECISION_STATE_MISMATCH")
            if proposed.get("ruling", {}).get("ruling_id") != existing.get("current_ruling_id"):
                raise Invalid("CANONICAL_RULING_MISMATCH")
            store.db.execute("INSERT OR IGNORE INTO decision_links VALUES (?, ?)", (item["id"], existing["id"]))
            continue
        decision = store.propose(item, proposed, migration=True)
        state = proposed.get("state", "owner_pending")
        if state in {"owner_decided", "later"}:
            store.ruling(decision, proposed["ruling"], state == "later")
        elif state != "owner_pending":
            raise Invalid("INVALID_MIGRATED_DECISION_STATE")
    states = {d["state"] for d in store.linked_decisions(item["id"])}
    if classification in {"owner_pending", "owner_decided", "later"} and classification not in states:
        raise Invalid("MISSING_CLASSIFIED_DECISION")
    if row.get("handoff"):
        handoff = row["handoff"]
        require(handoff, "id", "to_actor", "to_owner", "proposed_record", "authorization_ref", "instruction")
        accepted = handoff.get("acceptance_receipt")
        if accepted:
            require(accepted, "ref", "responsibility")
            if accepted.get("kind") != "accepted" or row["responsible_actor"] != handoff["to_actor"]:
                raise Invalid("UNVERIFIED_ACCEPTANCE")
        store.put("handoffs", {**handoff, "concern_id": item["id"], "from_actor": handoff.get("from_actor", row["responsible_actor"]),
                               "offer_version": 1, "version": 1, "state": "accepted" if accepted else "offered",
                               "delivery_receipts": handoff.get("delivery_receipts", []), "acceptance_receipt": accepted})
        item = store.change("concerns", store.get("concerns", item["id"]), routing={"state": "accepted" if accepted else "offered", "handoff_id": handoff["id"]})
    if classification == "route_only" and not row.get("handoff"):
        raise Invalid("ROUTE_MAPPING_NEEDS_HANDOFF")
    if classification in {"resolved", "not_applicable", "owner_dropped"}:
        if states & {"owner_pending", "later"}:
            raise Invalid("UNANSWERED_DECISION")
        closure = row.get("closure")
        require(closure, "kind", "ref", "evidence")
        if closure["kind"] != classification:
            raise Invalid("MAPPING_CLOSURE_MISMATCH")
        store.change("concerns", store.get("concerns", item["id"]), lifecycle="closed", closure=closure)


def migrate_additional(store, row):
    # Explicit new evidence from a hand-edited legacy view; not a synthesized old ID.
    require(row, "evidence_ref")
    data = row.get("concern")
    require(data, "id")
    item = store.intake(data, data.get("record_search", {}).get("result") == "found", migration=True)["item"]
    for decision in row.get("decisions", []):
        store.propose(item, decision, migration=True)


def load_json(path):
    try:
        value = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    except (OSError, ValueError) as error:
        raise Invalid("INVALID_JSON", detail=str(error))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Invalid("CLI_ARGUMENTS", detail=message)


def build_parser():
    parser = Parser(description=__doc__)
    common = Parser(add_help=False)
    for name in ("project", "actor-id", "by"):
        common.add_argument("--" + name, default=argparse.SUPPRESS)
    common.add_argument("--role", choices=("parent", "child"), default=argparse.SUPPRESS)
    common.add_argument("--text", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--project", default=None)
    parser.add_argument("--actor-id", default=os.environ.get("I_HAVE_OCD_ACTOR_ID", ""))
    parser.add_argument("--by", default=os.environ.get("I_HAVE_OCD_BY", ""))
    parser.add_argument("--role", choices=("parent", "child"), default="parent")
    parser.add_argument("--text", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def command(name, op, group=sub, data=False, identity=None, version=None, held=False, write=False):
        p = group.add_parser(name, parents=[common])
        p.set_defaults(op=op)
        if data:
            p.add_argument("--data", required=True)
        if identity:
            p.add_argument(identity)
        if version:
            p.add_argument("--expected-version", required=True, type=int)
            p.set_defaults(version_key=version)
        if held:
            p.add_argument("--token", required=True)
            p.add_argument("--fence", required=True, type=int)
        if write:
            p.add_argument("--request-id", required=True)
        return p

    command("capabilities", "capabilities")
    for op in ("intake", "link"):
        command(op, op, data=True, write=True)
    p = command("show", "show", identity="item_id")
    p.add_argument("--history", action="store_true")
    p.add_argument("--include-delivery", action="store_true")
    p = command("find", "find")
    p.add_argument("--key", required=True)
    p = command("list", "list")
    for name in ("owner", "state", "cursor"):
        p.add_argument("--" + name)
    p.add_argument("--include-counts", action="store_true")
    command("add-source", "add_source", data=True, identity="item_id", version="item", write=True)
    p = command("take", "lease.acquire", identity="item_id", version="item", write=True)
    p.add_argument("--ttl-minutes", type=int, default=60)
    for name in ("renew", "release"):
        p = command(name, "lease." + name, identity="item_id", version="item", held=True, write=True)
        p.add_argument("--ttl-minutes", type=int, default=60)
    p = sub.add_parser("main")
    group = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    command("get", "main.get", group)
    p = command("set", "main.set", group, version="main", write=True)
    for field in ("title", "done_when", "next_step", "paused_for", "set_by"):
        p.add_argument("--" + field.replace("_", "-"), default=None)
    p.add_argument("--note", action="append")
    for category, actions, identity, version in (
        ("route", ("offer", "accept", "reject", "cancel", "receipt"), "handoff_id", "handoff"),
        ("decision", ("propose", "link", "answer", "later", "reopen", "withdraw"), "decision_id", "decision"),
        ("review", ("start", "show", "present", "finish"), "review_id", "review"),
    ):
        p = sub.add_parser(category)
        group = p.add_subparsers(dest="action", required=True, parser_class=Parser)
        for action in actions:
            create = action in {"start", "offer", "propose"}
            ident = None if action == "start" else "item_id" if action in {"offer", "propose", "link"} else identity
            vers = None if action in {"start", "show"} else "item" if ident == "item_id" else version
            p = command(action, f"{category}.{action}", group, data=action != "show", identity=ident, version=vers,
                        held=action in {"offer", "cancel", "present", "finish"}, write=action != "show")
            if category == "decision" and action == "link":
                p.add_argument("--expected-decision-version", required=True, type=int)
    command("urgent", "urgent", data=True, identity="item_id", version="item", write=True)
    command("close", "close", data=True, identity="item_id", version="item", held=True, write=True)
    p = command("merge", "merge", data=True, identity="from_item", write=True)
    p.add_argument("--into", dest="to_item", required=True)
    for side in ("from", "to"):
        p.add_argument(f"--expected-{side}-version", required=True, type=int)
        p.add_argument(f"--{side}-token", required=True)
        p.add_argument(f"--{side}-fence", required=True, type=int)
    p = command("history", "history")
    p.add_argument("--after-event", type=int, default=0)
    p.add_argument("--cursor")
    command("render", "render", write=True)
    p = command("export", "export")
    p.add_argument("--include-history", action="store_true")
    p.add_argument("--after-event", type=int, default=0)
    for category in ("migrate", "import"):
        p = sub.add_parser(category)
        group = p.add_subparsers(dest="action", required=True, parser_class=Parser)
        for action in ("plan", "apply"):
            p = command(action, f"{category}.{action}", group, data=category == "import", write=action == "apply")
            if category == "migrate":
                p.add_argument("--source")
                p.add_argument("--mapping", required=True)
            if action == "apply":
                p.add_argument("--expected-revision", required=True, type=int)
    p = command("api", "api")
    p.add_argument("--request", required=True)
    p = sub.add_parser("backend")
    group = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    p = command("bind", "backend.bind", group)
    p.add_argument("--kind", required=True)
    for name in ("backend-id", "workspace-id", "cutover-epoch", "request-id"):
        p.add_argument("--" + name)
    return parser


def cli_request(args):
    if args.op == "api":
        return load_json(args.request)
    data = load_json(args.data) if hasattr(args, "data") else {}
    expected = {}
    if hasattr(args, "version_key"):
        expected[args.version_key] = args.expected_version
    for field in ("item_id", "decision_id", "handoff_id", "review_id", "from_item", "to_item", "owner", "state", "cursor", "key",
                  "history", "include_delivery", "include_counts", "ttl_minutes", "after_event", "include_history"):
        value = getattr(args, field, None)
        if value is not None:
            if field in data and data[field] != value:
                raise Invalid("CLI_TARGET_MISMATCH")
            data[field] = value
    if args.op == "main.set":
        data = {f: getattr(args, f) for f in ("title", "done_when", "next_step", "paused_for", "set_by") if getattr(args, f) is not None}
        if args.note is not None:
            data["notes"] = args.note
    if args.op == "import.apply":
        expected["revision"] = args.expected_revision
    if args.op == "decision.link":
        expected["decision"] = args.expected_decision_version
    request = {"contract": CONTRACT, "op": args.op, "actor": {"id": args.actor_id, "by": args.by, "parent_id": None},
               "input": data, "expected": expected}
    if hasattr(args, "request_id"):
        request["request_id"] = args.request_id
    if hasattr(args, "token"):
        request["lease"] = {"token": args.token, "fence": args.fence}
    if args.op == "merge":
        expected.update(from_item=args.expected_from_version, to_item=args.expected_to_version)
        request["leases"] = {"from_item": {"token": args.from_token, "fence": args.from_fence},
                             "to_item": {"token": args.to_token, "fence": args.to_fence}}
    return request


def main(argv=None):
    store = None
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        if any(v in {"park", "count", "decide", "done", "drop", "import-md"} for v in argv[:1]):
            raise Invalid("CLIENT_UPGRADE_REQUIRED")
        args = build_parser().parse_args(argv)
        backend_guard()
        store = Store(project_name(args.project), args.actor_id, args.by)
        if args.op == "backend.bind":
            role_guard({"id": args.actor_id}, args.role, True)
            raise Unavailable("SHARED_BACKEND_UNSUPPORTED" if args.kind == "shared" else "BINDING_UNSUPPORTED")
        if args.op.startswith("migrate."):
            result = migrate(store, args.action, args.source, load_json(args.mapping), getattr(args, "expected_revision", None),
                             getattr(args, "request_id", None), args.role)
        else:
            result = store.execute(cli_request(args), args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.text else None))
        return 0
    except (Failure, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        failure = error if isinstance(error, Failure) else Unavailable("STORE_ERROR", detail=str(error))
        print(json.dumps({"ok": False, "contract": CONTRACT, "error": {"code": failure.kind, "reason": failure.reason,
                          "retryable": False}, "committed": failure.state.pop("committed", False), **failure.state}))
        return failure.code
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
