"""Real CLI, concurrent processes, crash recovery and format-1 migration, all in temp stores."""
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/engineering/i-have-ocd/scripts/ocd.py"
OLD_REF = "b6e09b8"  # assignment's origin/main base; never follow a moving remote in a fixture


def env(home, **extra):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "I_HAVE_OCD_HOME": str(home / "state"), "I_HAVE_OCD_PROJECT": "sample",
            "I_HAVE_OCD_ACTOR_ID": "actor-a", **extra}


def start(argv, environment, script=SCRIPT, cwd=None):
    return subprocess.Popen([sys.executable, str(script), *map(str, argv)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=environment, cwd=cwd)


def run(argv, environment, script=SCRIPT, cwd=None):
    process = start(argv, environment, script, cwd)
    out, err = process.communicate(timeout=60)
    return process.returncode, json.loads(out) if out.startswith("{") else out, err


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canon(value).encode()).hexdigest()


def finding(n=1, actor="actor-a"):
    return {"key": {"object": "component:reader", "consequence": "records-rejected", "occurrence": f"release-{n}"},
            "summary": "Reader rejects valid input", "trigger": "Probe failed", "consequence": "Records cannot be imported",
            "sources": [{"ref": f"probe-{n}", "evidence": "Input and error output"}], "owner": "maintenance",
            "responsible_actor": actor, "record_search": {"result": "none", "checked": ["task", "index"], "evidence_ref": "search"}}


def choice(n=1):
    return {"question": f"Extend support {n}?", "owner_only_reason": "Exceeds current scope", "authority_ref": "task",
            "options": [{"id": "keep", "label": "Keep", "cost": "No new support"}, {"id": "extend", "label": "Extend", "cost": "More work"}],
            "recommended": "keep", "inaction_consequence": "No new support"}


def request_file(home, op, data, expected=None, actor="actor-a", request_id="request-1", lease=None):
    request = {"contract": "i-have-ocd/1", "op": op, "actor": {"id": actor, "parent_id": None},
               "request_id": request_id, "input": data, "expected": expected or {}}
    if lease:
        request["lease"] = lease
    path = home / f"{request_id}.json"
    path.write_text(json.dumps(request))
    return path


def api(home, environment, op, data, **kwargs):
    path = request_file(home, op, data, **kwargs)
    return run(["api", "--request", path], environment)


def test_concurrent_same_key_creates_one_concern(tmp_path):
    environment = env(tmp_path)
    processes = []
    for n in range(12):
        path = request_file(tmp_path, "intake", finding(actor=f"actor-{n}"), actor=f"actor-{n}", request_id=f"intake-{n}")
        processes.append(start(["api", "--request", path], environment))
    results = [p.communicate(timeout=60) for p in processes]
    assert all(p.returncode == 0 for p in processes), results
    items = [json.loads(out)["result"]["item"] for out, _ in results]
    assert len({i["id"] for i in items}) == 1
    assert len({i["responsible_actor"] for i in items}) == 1
    outcomes = [json.loads(out)["result"]["outcome"] for out, _ in results]
    assert outcomes.count("created") == 1 and outcomes.count("existing") == 11
    code, listed, err = run(["list"], environment)
    assert code == 0 and len(listed["result"]["items"]) == 1, err
    assert "counts" not in listed["result"]


def test_concurrent_take_and_review_share_one_fenced_resource(tmp_path):
    environment = env(tmp_path)
    item = api(tmp_path, environment, "intake", finding())[1]["result"]["item"]
    result = api(tmp_path, environment, "decision.propose", {"item_id": item["id"], **choice()}, expected={"item": 1}, request_id="decision")[1]["result"]
    processes = [start(["take", item["id"], "--expected-version", 2, "--request-id", f"take-{n}", "--actor-id", f"actor-{n}"], environment) for n in range(6)]
    results = [p.communicate(timeout=60) for p in processes]
    assert sorted(p.returncode for p in processes) == [0, 3, 3, 3, 3, 3], results
    paths = [request_file(tmp_path, "review.start", {"owner_scope": "owner", "trigger": {"kind": "owner_request", "ref": "same-review"}},
             actor=f"actor-{n}", request_id=f"review-{n}") for n in range(6)]
    processes = [start(["api", "--request", path], environment) for path in paths]
    results = [p.communicate(timeout=60) for p in processes]
    assert all(p.returncode == 0 for p in processes), results
    reviews = [json.loads(out)["result"] for out, _ in results]
    assert len({r["review"]["id"] for r in reviews}) == 1
    assert sum("lease" in r for r in reviews) == 1
    assert reviews[0]["review"]["snapshot"][0]["id"] == result["decision"]["id"]


KILLER = '''
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location("ocd", sys.argv[1])
ocd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocd)
original = ocd.Store.commit
def killed(self):
    if sys.argv[2] == "after":
        original(self)
    os.kill(os.getpid(), signal.SIGKILL)
ocd.Store.commit = killed
sys.exit(ocd.main(sys.argv[3:]))
'''


def test_disk_crash_rolls_back_or_replays_atomic_request_receipt(tmp_path):
    environment = env(tmp_path)
    # Create before the kill so that it interrupts a mutation, not schema creation.
    assert api(tmp_path, environment, "intake", finding(), request_id="initial")[0] == 0
    for n, when in ((2, "before"), (3, "after")):
        path = request_file(tmp_path, "intake", finding(n), request_id=f"crash-{n}")
        killed = subprocess.run([sys.executable, "-c", KILLER, str(SCRIPT), when, "api", "--request", str(path)],
                                env=environment, capture_output=True, text=True, timeout=60)
        assert killed.returncode == -signal.SIGKILL, killed.stderr
        code, retry, err = run(["api", "--request", path], environment)
        assert code == 0 and retry["replayed"] == (when == "after"), err
    listed = run(["list"], environment)[1]
    assert len(listed["result"]["items"]) == 3
    with sqlite3.connect(tmp_path / "state/sample.db") as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3
    assert not (tmp_path / "state/sample.md").exists()  # views are explicit, never read-time writes
    assert run(["render", "--request-id", "render-1"], environment)[0] == 0


def old_helper(home):
    result = subprocess.run(["git", "show", f"{OLD_REF}:skills/engineering/i-have-ocd/scripts/ocd.py"], cwd=ROOT,
                            capture_output=True, text=True, check=True, timeout=10)
    path = home / "format-1.py"
    path.write_text(result.stdout)
    return path


def old_store(home):
    helper = old_helper(home)
    environment = env(home, I_HAVE_OCD_BY="actor-a")
    for n in range(1, 5):
        argv = ["park", f"Neutral finding {n}", "--source", f"probe-{n}", "--request-id", f"old-{n}"]
        if n == 2:
            argv.append("--needs-owner")
        assert run(argv, environment, helper)[0] == 0
    assert run(["main", "set", "--title", "Legacy task", "--next-step", "Run probe", "--expected-version", 0,
                "--request-id", "old-main"], environment, helper)[0] == 0
    assert run(["take", "P1", "--expected-version", 1, "--request-id", "old-lease"], environment, helper)[0] == 0
    return helper, environment


def make_mapping(home, environment):
    path = home / "mapping.json"
    path.write_text('{"items": []}')
    code, result, err = run(["migrate", "plan", "--mapping", path], environment)
    assert code == 0, err
    plan = result["result"]
    rows = []
    for n, source in enumerate(plan["items"], 1):
        data = finding(n)
        identity = f"concern-{n}"
        record = f"task:record-{n}"
        data["record_search"]["result"] = "found"
        data["owning_record"] = {"ref": record, "owner": "maintenance", "authority_ref": "task-authority"}
        row = {"origin_store_id": plan["origin_store_id"], "legacy_id": source["legacy_id"], "old_version": source["old_version"],
               "raw_hash": source["raw_hash"], "classification": "engineering", "concern_id": identity, "concern": data,
               "decision_ids": [], "decisions": [], "owner": "maintenance", "responsible_actor": "actor-a", "owning_record": record,
               "disposition": "Keep in the responsible lane", "receipt_refs": ["task-authority"], "uncertainty": []}
        if n == 1:
            row["classification"] = "route_only"
            row["handoff"] = {"id": "handoff-1", "to_actor": "actor-b", "to_owner": "reader team", "proposed_record": "task:reader",
                              "authorization_ref": "task-authority", "instruction": "Take the existing record"}
        if n == 2:
            row["classification"] = "owner_pending"
            row["decisions"] = [{**choice(), "id": "decision-2", "state": "owner_pending"}]
            row["decision_ids"] = ["decision-2"]
        if n == 3:
            row["classification"] = "later"
            row["decisions"] = [{**choice(3), "id": "decision-3", "state": "later", "ruling": {"answer_ref": "original-later", "ruling_id": "ruling-3",
                "reopen_condition": {"kind": "owner_request", "ref": "scope", "predicate": "Owner explicitly reopens"}}}]
            row["decision_ids"] = ["decision-3"]
        if n == 4:
            row["classification"] = "not_applicable"
            row["closure"] = {"kind": "not_applicable", "ref": "verification-4", "evidence": "The affected format is not used"}
        rows.append(row)
    mapping = {"items": rows}
    path.write_text(json.dumps(mapping))
    return path, plan, mapping


def test_real_format_one_migration_retains_history_and_old_client_is_fenced(tmp_path):
    helper, environment = old_store(tmp_path)
    path, plan, mapping = make_mapping(tmp_path, environment)
    db_path = tmp_path / "state/sample.db"
    before = db_path.read_bytes()
    code, planned, err = run(["migrate", "plan", "--mapping", path], environment)
    assert code == 0 and planned["result"]["ready"], err
    assert db_path.read_bytes() == before
    assert run(["list"], environment)[1]["error"]["reason"] == "MIGRATION_REQUIRED"
    assert run(["migrate", "apply", "--mapping", path, "--expected-revision", plan["revision"] - 1, "--request-id", "migration"], environment)[0] == 3
    code, applied, err = run(["migrate", "apply", "--mapping", path, "--expected-revision", plan["revision"], "--request-id", "migration"], environment)
    assert code == 0 and applied["result"]["leases_fenced"], (applied, err)
    replay = run(["migrate", "apply", "--mapping", path, "--expected-revision", plan["revision"], "--request-id", "migration"], environment)
    assert replay[0] == 0 and replay[1]["replayed"] and replay[1]["receipt"] == applied["receipt"]
    assert run(["park", "obsolete", "--request-id", "old-after"], environment, helper)[0] == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM aliases").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM legacy_events").fetchone()[0] == plan["revision"]
        assert db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0
        assert db.execute("SELECT title FROM legacy_main_line").fetchone()[0] == "Legacy task"
    first = run(["show", "P1", "--include-delivery"], environment)[1]["result"]["item"]
    assert first["routing"]["state"] == "offered" and first["decisions"] == []
    assert first["responsible_actor"] == "actor-a"
    second = run(["show", "P2"], environment)[1]["result"]["item"]
    assert second["attention_state"] == "owner_pending"
    assert run(["show", "P3"], environment)[1]["result"]["item"]["attention_state"] == "later"
    assert run(["show", "P4"], environment)[1]["result"]["item"]["lifecycle"] == "closed"
    assert run(["main", "get"], environment)[1]["result"]["main"] is None
    export = run(["export", "--include-history"], environment)[1]["result"]
    assert "claim_token" not in json.dumps(export) and "\"token\"" not in json.dumps(export)
    assert len(export["snapshot"]["legacy"]["legacy_items"]) == 4
    backup = tmp_path / "state" / applied["result"]["backup"]
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 4


def test_migration_reads_live_wal_and_refuses_unmapped_markdown(tmp_path):
    _, environment = old_store(tmp_path)
    db_path = tmp_path / "state/sample.db"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("UPDATE items SET summary='Evidence in live WAL', version=version+1 WHERE id=2")
        writer.commit()
        assert db_path.with_name(db_path.name + "-wal").exists()
        path, plan, mapping = make_mapping(tmp_path, environment)
        assert plan["items"][1]["raw"]["summary"] == "Evidence in live WAL"
        view = tmp_path / "state/sample.md"
        edited = "\n".join(line for line in view.read_text().splitlines() if not line.startswith("- P2 ·")) + "\n"
        view.write_text(edited)
        code, result, _ = run(["migrate", "plan", "--mapping", path], environment)
        assert code == 0 and not result["result"]["ready"] and result["result"]["markdown_diff"]
        assert run(["migrate", "apply", "--mapping", path, "--expected-revision", plan["revision"], "--request-id", "apply"], environment)[0] == 2
        mapping["view_resolution"] = {"hash": digest(edited.encode()), "evidence_ref": "manual-diff-review", "disposition": "Deletion is not closure; keep every mapped concern"}
        path.write_text(json.dumps(mapping))
        assert run(["migrate", "apply", "--mapping", path, "--expected-revision", plan["revision"], "--request-id", "apply"], environment)[0] == 0
        assert run(["show", "P2"], environment)[1]["result"]["item"]["needs_owner"]
        assert view.read_text() == edited
    finally:
        writer.close()


def test_bad_mapping_rolls_back_every_schema_and_history_change(tmp_path):
    _, environment = old_store(tmp_path)
    path, plan, mapping = make_mapping(tmp_path, environment)
    mapping["items"][-1]["closure"]["kind"] = "transferred"
    path.write_text(json.dumps(mapping))
    code, result, _ = run(["migrate", "apply", "--mapping", path, "--expected-revision", plan["revision"], "--request-id", "apply"], environment)
    assert code == 2 and result["committed"] is False
    with sqlite3.connect(tmp_path / "state/sample.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 4
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='concerns'").fetchall()


def test_api_migration_uses_the_same_role_cas_and_replay_checks(tmp_path):
    _, environment = old_store(tmp_path)
    _, plan, mapping = make_mapping(tmp_path, environment)
    data = {"mapping": mapping}
    child = env(tmp_path, I_HAVE_OCD_CHILD="1")
    assert api(tmp_path, child, "migrate.apply", data, expected={"revision": plan["revision"]}, request_id="api-migration")[0] == 4
    code, result, err = api(tmp_path, environment, "migrate.plan", data, request_id="api-plan")
    assert code == 0 and result["result"]["ready"], (result, err)
    code, result, err = api(tmp_path, environment, "migrate.apply", data, expected={"revision": plan["revision"]}, request_id="api-migration")
    assert code == 0, (result, err)
    code, replay, err = api(tmp_path, environment, "migrate.apply", data, expected={"revision": plan["revision"]}, request_id="api-migration")
    assert code == 0 and replay["replayed"], (replay, err)


@pytest.mark.parametrize("when", ["before", "after"])
def test_migration_crash_is_atomic_and_can_resume(tmp_path, when):
    _, environment = old_store(tmp_path)
    path, plan, _ = make_mapping(tmp_path, environment)
    argv = ["migrate", "apply", "--mapping", str(path), "--expected-revision", str(plan["revision"]), "--request-id", "crash-migrate"]
    process = subprocess.run([sys.executable, "-c", KILLER, str(SCRIPT), when, *argv], env=environment,
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == -signal.SIGKILL, process.stderr
    code, result, err = run(argv, environment)
    assert code == 0 and result["replayed"] == (when == "after"), (result, err)
    with sqlite3.connect(tmp_path / "state/sample.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM concerns").fetchone()[0] == 4


def test_cli_round_trip_and_stdin_payload(tmp_path):
    environment = env(tmp_path)
    process = subprocess.run([sys.executable, str(SCRIPT), "intake", "--data", "-", "--request-id", "cli-intake"],
                             env=environment, input=json.dumps(finding()), capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr
    item = json.loads(process.stdout)["result"]["item"]
    decision_file = tmp_path / "choice.json"
    decision_file.write_text(json.dumps(choice()))
    code, proposed, err = run(["decision", "propose", item["id"], "--data", decision_file, "--expected-version", 1, "--request-id", "cli-choice"], environment)
    assert code == 0, (proposed, err)
    decision = proposed["result"]["decision"]
    assert proposed["result"]["item_id"] == item["id"] and proposed["result"]["item_version"] == 2
    assert proposed["result"]["decision_id"] == decision["id"] and proposed["result"]["decision_version"] == 1
    assert proposed["result"]["attention_state"] == "owner_pending" and proposed["result"]["needs_owner"]
    answer_file = tmp_path / "answer.json"
    answer_file.write_text(json.dumps({"option_id": "keep", "answer_ref": "owner-answer", "ruling_id": "cli-ruling"}))
    assert run(["decision", "answer", decision["id"], "--data", answer_file, "--expected-version", 1, "--request-id", "cli-answer"], environment)[0] == 0
    taken = run(["take", item["id"], "--expected-version", 2, "--request-id", "cli-take"], environment)[1]["result"]["lease"]
    close_file = tmp_path / "close.json"
    close_file.write_text(json.dumps({"kind": "resolved", "ref": "verified-fix", "evidence": "Contract passes"}))
    assert run(["close", item["id"], "--data", close_file, "--expected-version", 2, "--token", taken["token"], "--fence", taken["fence"], "--request-id", "cli-close"], environment)[0] == 0
    assert run(["show", item["id"], "--history"], environment)[1]["result"]["item"]["lifecycle"] == "closed"
    exported = run(["export", "--include-history"], environment)[1]["result"]
    export_file = tmp_path / "export.json"
    export_file.write_text(json.dumps(exported))
    target = env(tmp_path, I_HAVE_OCD_PROJECT="restored")
    assert run(["import", "plan", "--data", export_file], target)[0] == 0
    assert not (tmp_path / "state/restored.db").exists()
    assert run(["import", "apply", "--data", export_file, "--expected-revision", 0, "--request-id", "cli-import"], target)[0] == 0
    assert run(["show", item["id"]], target)[1]["result"]["item"]["lifecycle"] == "closed"


def test_cli_child_and_backend_binding_refuse_before_store_creation(tmp_path):
    path = tmp_path / "finding.json"
    path.write_text(json.dumps(finding()))
    for environment, flags in ((env(tmp_path, I_HAVE_OCD_CHILD="1"), []), (env(tmp_path), ["--role", "child"])):
        code, result, _ = run(["intake", "--data", path, "--request-id", "child", *flags], environment)
        assert code == 4 and result["error"]["reason"] == "RETURN_TO_PARENT"
    code, result, _ = run(["backend", "bind", "--kind", "shared"], env(tmp_path))
    assert code == 1 and result["error"]["code"] == "UNAVAILABLE"
    assert not (tmp_path / "state").exists()


def test_every_worktree_uses_one_project_store(tmp_path):
    repo, worktree = tmp_path / "sample-repo", tmp_path / "feature"
    environment = env(tmp_path, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    environment.pop("I_HAVE_OCD_PROJECT")
    for argv in (["init", "-q", str(repo)], ["-C", str(repo), "commit", "-q", "--allow-empty", "-m", "fixture"],
                 ["-C", str(repo), "worktree", "add", "-q", "-b", "feature", str(worktree)]):
        subprocess.run(["git", *argv], check=True, env=environment, capture_output=True, timeout=10)
    path = request_file(tmp_path, "intake", finding())
    assert run(["api", "--request", path], environment, cwd=worktree)[0] == 0
    assert (tmp_path / "state/sample-repo.db").exists()
    assert len(run(["list"], environment, cwd=repo)[1]["result"]["items"]) == 1
