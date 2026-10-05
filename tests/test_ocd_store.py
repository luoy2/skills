"""Single-process contract tests with neutral data. Process tests live in integration/."""
import copy
import importlib.util
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/engineering/i-have-ocd/scripts/ocd.py"


@pytest.fixture(scope="module")
def ocd():
    spec = importlib.util.spec_from_file_location("ocd_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("I_HAVE_OCD_HOME", str(tmp_path))
    monkeypatch.setenv("I_HAVE_OCD_PROJECT", "sample")
    monkeypatch.setenv("I_HAVE_OCD_ACTOR_ID", "actor-a")
    monkeypatch.delenv("I_HAVE_OCD_BACKEND", raising=False)
    monkeypatch.delenv("I_HAVE_OCD_CHILD", raising=False)
    return tmp_path


def finding(n=1, actor="actor-a", owner="maintenance"):
    return {"key": {"object": "component:reader", "consequence": "valid-input-rejected", "occurrence": f"incident-{n}"},
            "summary": "Reader rejects valid input", "trigger": "Contract probe failed", "consequence": "Records cannot be imported",
            "sources": [{"ref": f"run-{n}", "evidence": "Probe input and rejected output"}], "owner": owner,
            "responsible_actor": actor, "record_search": {"result": "none", "checked": ["task", "record-index"], "evidence_ref": "search-1"}}


def choice(n=1, **extra):
    return {"question": f"Support optional format {n}?", "owner_only_reason": "Changes the authorized scope",
            "authority_ref": "task-1", "options": [{"id": "keep", "label": "Keep scope", "cost": "Format remains unsupported"},
            {"id": "extend", "label": "Extend scope", "cost": "Adds implementation and validation"}],
            "recommended": "keep", "inaction_consequence": "Format cannot enter this delivery", **extra}


@pytest.fixture
def api(ocd, home, capsys):
    sequence = 0

    def run(op, data=None, expected=None, actor="actor-a", request_id=None, lease=None, leases=None, role="parent", workspace=None):
        nonlocal sequence
        sequence += 1
        envelope = {"contract": ocd.CONTRACT, "op": op, "actor": {"id": actor, "parent_id": None},
                    "input": data or {}, "expected": expected or {}, "request_id": request_id or f"request-{sequence}"}
        if workspace:
            envelope["workspace_id"] = workspace
        if lease:
            envelope["lease"] = lease
        if leases:
            envelope["leases"] = leases
        path = home / "request.json"
        path.write_text(json.dumps(envelope))
        code = ocd.main(["api", "--request", str(path), "--role", role])
        return code, json.loads(capsys.readouterr().out)
    return run


def ok(api, *args, **kwargs):
    code, result = api(*args, **kwargs)
    assert code == 0, result
    return result["result"]


def create(api, n=1, **kwargs):
    return ok(api, "intake", finding(n, **kwargs))["item"]


def propose(api, item, n=1, **extra):
    return ok(api, "decision.propose", {"item_id": item["id"], **choice(n, **extra)}, {"item": item["version"]})


def take(api, item, actor="actor-a"):
    return ok(api, "lease.acquire", {"item_id": item["id"]}, {"item": item["version"]}, actor=actor)["lease"]


def shown(api, item):
    return ok(api, "show", {"item_id": item["id"]})["item"]


def test_intake_requires_consequence_and_no_owning_record(api):
    invalid = finding()
    invalid["consequence"] = " "
    code, error = api("intake", invalid)
    assert code == 2 and error["error"]["reason"] == "NO_CONCRETE_CONSEQUENCE"
    invalid = finding()
    invalid["record_search"]["result"] = "found"
    invalid["owning_record"] = {"ref": "task:existing", "owner": "maintenance", "authority_ref": "task-authority"}
    code, error = api("intake", invalid)
    assert code == 2 and error["error"]["reason"] == "USE_EXISTING_RECORD"
    assert ok(api, "list")["items"] == []
    linked = ok(api, "link", invalid)
    assert linked["outcome"] == "linked" and linked["item"]["attention_state"] == "routed"
    assert linked["item"]["decisions"] == []
    invalid["owning_record"]["ref"] = "task:another"
    assert api("link", invalid)[0] == 3


def test_key_is_normalized_and_deduplication_never_changes_responsibility(api):
    data = finding()
    data["key"]["object"] = "  cafe\u0301  "
    first = ok(api, "intake", data)["item"]
    data["key"]["object"] = "café"
    data["responsible_actor"] = "actor-b"
    data["owner"] = "another lane"
    again = ok(api, "intake", data, actor="actor-b")
    assert again["outcome"] == "existing" and again["item"]["id"] == first["id"]
    assert again["item"]["responsible_actor"] == "actor-a"
    different = finding(2)
    different["key"]["consequence"] = "delayed-output"
    assert ok(api, "intake", different)["item"]["id"] != first["id"]


def test_sqlite_rejects_a_duplicate_concern_key(api, home, ocd):
    item = create(api)
    with sqlite3.connect(home / "sample.db") as db:
        workspace = db.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()[0]
        duplicate = {**item, "id": ocd.new_id()}
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO concerns VALUES (?, ?, ?, ?)",
                       (duplicate["id"], workspace, item["concern_key"], ocd.canonical(duplicate)))
    assert len(ok(api, "list")["items"]) == 1


def test_request_id_cas_and_actor_are_checked_atomically(api):
    first = ok(api, "intake", finding(), request_id="same")
    code, replay = api("intake", finding(), request_id="same")
    assert code == 0 and replay["replayed"] and replay["result"] == first
    assert api("intake", finding(2), request_id="same")[0] == 3
    assert api("intake", finding(actor="actor-b"), request_id="same", actor="actor-b")[0] == 3
    item = first["item"]
    source = {"item_id": item["id"], "sources": [{"ref": "run-new", "evidence": "New consumer fails"}]}
    changed = ok(api, "add_source", source, {"item": 1})["item"]
    assert changed["id"] == item["id"] and changed["version"] == 2
    code, error = api("add_source", source, {"item": 1})
    assert code == 3 and error["current"]["version"] == 2
    assert len(shown(api, item)["sources"]) == 2
    assert api("add_source", source, {"item": 2, "decision": 0})[0] == 2
    assert api("show", {"item_id": item["id"]}, workspace="wrong-workspace")[0] == 4


def test_route_and_ack_never_clear_owner_decisions(api):
    result = propose(api, create(api))
    item, decision = result["item"], result["decision"]
    lease = take(api, item)
    offered = ok(api, "route.offer", {"item_id": item["id"], "target": {"actor_id": "actor-b", "owner": "reader team"},
                "proposed_record": "task:reader", "authorization_ref": "existing-task", "instruction": "Take responsibility on the existing record"},
                {"item": item["version"]}, lease=lease)
    assert offered["delivery"] == "manual" and offered["item"]["responsible_actor"] == "actor-a"
    handoff = offered["handoff"]
    ack = ok(api, "route.receipt", {"handoff_id": handoff["id"], "receipt": {"kind": "ack", "ref": "message-1"}},
             {"handoff": 1}, actor="actor-b")
    assert ack["item"]["needs_owner"] and ack["item"]["responsible_actor"] == "actor-a"
    accept = {"handoff_id": handoff["id"], "offer_version": 1, "owning_record": "task:reader",
              "receipt": {"kind": "accepted", "ref": "acceptance-1", "responsibility": "Own follow-up including the choice"}}
    assert api("route.accept", accept, {"handoff": 2}, actor="actor-c")[0] == 4
    bad = copy.deepcopy(accept)
    bad["receipt"]["kind"] = "ack"
    assert api("route.accept", bad, {"handoff": 2}, actor="actor-b")[0] == 2
    accepted = ok(api, "route.accept", accept, {"handoff": 2}, actor="actor-b")["item"]
    assert accepted["id"] == item["id"] and accepted["responsible_actor"] == "actor-b"
    assert accepted["routing"]["state"] == "accepted" and accepted["attention_state"] == "owner_pending"
    assert accepted["decisions"][0]["id"] == decision["id"]
    new_lease = take(api, accepted, actor="actor-b")
    assert new_lease["fence"] > lease["fence"]
    ok(api, "decision.answer", {"decision_id": decision["id"], "option_id": "keep", "answer_ref": "owner-answer-1", "ruling_id": "ruling-1"}, {"decision": 1})
    accepted = shown(api, item)
    assert accepted["attention_state"] == "owner_decided" and accepted["lifecycle"] == "active"


def test_close_cannot_drop_unanswered_or_later_decisions(api):
    result = propose(api, create(api))
    item, decision = result["item"], result["decision"]
    lease = take(api, item)
    closure = {"item_id": item["id"], "kind": "owner_dropped", "ref": "owner-drop", "evidence": "Owner abandoned the work"}
    assert api("close", closure, {"item": item["version"]}, lease=lease)[0] == 3
    condition = {"kind": "dependency_completed", "ref": "dependency-1", "predicate": "Supported reader released"}
    ok(api, "decision.later", {"decision_id": decision["id"], "answer_ref": "owner-later", "ruling_id": "later-1", "reopen_condition": condition}, {"decision": 1})
    assert api("close", closure, {"item": item["version"]}, lease=lease)[0] == 3
    bad = {"decision_id": decision["id"], "evidence_ref": "message-ack", "reason": "Already received"}
    assert api("decision.withdraw", bad, {"decision": 2})[0] == 2
    ok(api, "decision.withdraw", {**bad, "choice_no_longer_exists": True, "basis": "choice_removed", "evidence_ref": "scope-removed"}, {"decision": 2})
    closed = ok(api, "close", closure, {"item": item["version"]}, lease=lease)["item"]
    assert closed["lifecycle"] == "closed" and closed["decisions"][0]["state"] == "withdrawn"


def test_merge_preserves_every_unanswered_decision_and_sources(api):
    a = propose(api, create(api), 1)["item"]
    b = propose(api, create(api, 2), 2)["item"]
    leases = {"from_item": take(api, a), "to_item": take(api, b)}
    payload = {"from_item": a["id"], "to_item": b["id"], "evidence_ref": "comparison", "reason": "Evidence confirms the same occurrence"}
    assert api("merge", payload, {"from_item": a["version"], "to_item": 99}, leases=leases)[0] == 3
    assert shown(api, a)["id"] == a["id"]
    merged = ok(api, "merge", payload, {"from_item": a["version"], "to_item": b["version"]}, leases=leases)["item"]
    assert len(merged["decisions"]) == 2 and all(d["state"] == "owner_pending" for d in merged["decisions"])
    assert len(merged["sources"]) == 2
    assert shown(api, a)["id"] == b["id"]
    assert ok(api, "find", {"key": finding()["key"]})["item"]["id"] == b["id"]
    assert ok(api, "intake", finding())["item"]["id"] == b["id"]


def test_stale_fence_cannot_write_even_with_current_item_version(api, ocd, monkeypatch):
    item = create(api)
    original = take(api, item)
    later = ocd.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(ocd, "utcnow", lambda: later)
    current = take(api, item)
    assert current["fence"] == original["fence"] + 1
    closure = {"item_id": item["id"], "kind": "resolved", "ref": "fix", "evidence": "Probe passes"}
    assert api("close", closure, {"item": 1}, lease={**current, "fence": original["fence"]})[0] == 3
    assert api("close", closure, {"item": 1}, lease=original)[0] == 3
    assert api("lease.renew", {"item_id": item["id"]}, {"item": 1}, lease=original)[0] == 3
    assert api("close", closure, {"item": 1}, lease=current, actor="actor-b")[0] in (3, 4)
    assert ok(api, "close", closure, {"item": 1}, lease=current)["item"]["lifecycle"] == "closed"
    read = ok(api, "show", {"item_id": item["id"], "history": True})
    assert "token" not in json.dumps(read)


def test_one_review_presents_every_pending_decision_across_all_lanes(api, ocd, monkeypatch):
    for n in range(6):
        item = create(api, n, owner=f"lane-{n % 3}")
        propose(api, item, n, group="same-group")
    start = {"owner_scope": "owner", "trigger": {"kind": "owner_request", "ref": "request-review-1"}}
    first = ok(api, "review.start", start)
    review = first["review"]
    assert len(review["snapshot"]) == len(review["decisions"]) == 6 and "has_more" not in review
    resumed = ok(api, "review.start", start, actor="actor-b")
    assert resumed["resumed"] and resumed["review"]["id"] == review["id"] and "lease" not in resumed
    present = {"review_id": review["id"], "presentation_ref": "questionnaire-1"}
    assert api("review.present", present, {"review": 1}, lease=first["lease"], actor="actor-b")[0] == 3
    presented = ok(api, "review.present", present, {"review": 1}, lease=first["lease"])["review"]
    assert len(presented["presented_ids"]) == 6
    clock = ocd.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(ocd, "utcnow", lambda: clock)
    retaken = ok(api, "review.start", start, actor="actor-b")
    assert retaken["review"]["id"] == review["id"] and len(retaken["review"]["snapshot"]) == 6
    assert retaken["lease"]["fence"] > first["lease"]["fence"]
    assert api("review.finish", {"review_id": review["id"], "finish_ref": "finished"}, {"review": 2}, lease=first["lease"])[0] == 3
    ok(api, "review.finish", {"review_id": review["id"], "finish_ref": "finished"}, {"review": 2}, actor="actor-b", lease=retaken["lease"])
    assert api("review.start", start)[0] == 3
    assert len(ok(api, "list")["items"]) == 6
    next_review = ok(api, "review.start", {**start, "trigger": {"kind": "owner_request", "ref": "request-review-2"}})
    assert next_review["review"]["id"] != review["id"]


def test_review_does_not_refill_a_changed_snapshot(api):
    item = create(api)
    decisions = []
    for n in range(4):
        proposed = propose(api, item, n)
        item = proposed["item"]
        decisions.append(proposed["decision"])
    started = ok(api, "review.start", {"owner_scope": "owner", "trigger": {"kind": "main_line_blocker", "ref": "blocker"}})
    late = propose(api, item, 4)["decision"]
    decision = decisions[0]
    ok(api, "decision.answer", {"decision_id": decision["id"], "option_id": "keep", "ruling_id": "ruling", "answer_ref": "answer"}, {"decision": 1})
    current = ok(api, "review.show", {"review_id": started["review"]["id"]})["review"]
    assert len(current["decisions"]) == 3 and len(current["snapshot"]) == 4
    assert late["id"] not in {d["id"] for d in current["decisions"]}
    assert "token" not in json.dumps(current)


def test_later_reopens_only_for_matching_event_and_keeps_rulings(api, ocd, monkeypatch):
    result = propose(api, create(api))
    decision = result["decision"]
    condition = {"kind": "consequence_changed", "ref": "capacity", "predicate": "New evidence changes the limit"}
    assert api("decision.later", {"decision_id": decision["id"], "answer_ref": "silence", "ruling_id": "ruling"}, {"decision": 1})[0] == 2
    ok(api, "decision.later", {"decision_id": decision["id"], "answer_ref": "owner-response", "ruling_id": "later-ruling", "reopen_condition": condition}, {"decision": 1})
    clock = ocd.utcnow() + timedelta(days=365)
    monkeypatch.setattr(ocd, "utcnow", lambda: clock)
    assert shown(api, result["item"])["attention_state"] == "later"
    assert ok(api, "list", {"state": "owner_pending"})["items"] == []
    reopen = {"decision_id": decision["id"], "event_ref": "new-probe", "evidence_ref": "probe-output", "incremental_question": "Increase the measured limit?",
              "event_kind": "consequence_changed", "condition_ref": "capacity", "predicate": condition["predicate"]}
    assert api("decision.reopen", {**reopen, "event_kind": "time_elapsed"}, {"decision": 2})[0] == 2
    ok(api, "decision.reopen", reopen, {"decision": 2})
    ok(api, "decision.answer", {"decision_id": decision["id"], "option_id": "extend", "ruling_id": "final-ruling", "answer_ref": "second-answer"}, {"decision": 3})
    snapshot = ok(api, "export", {"include_history": True})["snapshot"]
    assert len(snapshot["tables"]["rulings"]) == 2


def test_shared_decision_links_reuse_the_original_ruling(api):
    result = propose(api, create(api))
    decision = result["decision"]
    ok(api, "decision.answer", {"decision_id": decision["id"], "option_id": "keep", "answer_ref": "owner-answer", "ruling_id": "one-ruling"}, {"decision": 1})
    second = create(api, 2)
    linked = ok(api, "decision.link", {"item_id": second["id"], "decision_id": decision["id"], "evidence_ref": "same-choice"}, {"item": 1, "decision": 2})
    assert linked["item"]["attention_state"] == "owner_decided"
    assert linked["item"]["decisions"][0]["current_ruling_id"] == "one-ruling"
    assert len(ok(api, "export", {"include_history": True})["snapshot"]["tables"]["rulings"]) == 1


def test_recurrence_requires_new_occurrence_after_a_real_fix(api):
    item = create(api)
    lease = take(api, item)
    ok(api, "close", {"item_id": item["id"], "kind": "resolved", "ref": "fix-1", "evidence": "Contract probe passes"}, {"item": 1}, lease=lease)
    assert ok(api, "intake", finding())["item"]["id"] == item["id"]
    data = {**finding(2), "recurrence_of": item["id"]}
    recurrence = ok(api, "intake", data)["item"]
    assert recurrence["id"] != item["id"] and recurrence["recurrence_of"] == item["id"]
    data = {**finding(3), "recurrence_of": recurrence["id"]}
    assert api("intake", data)[0] == 2
    assert ok(api, "add_source", {"item_id": item["id"], "sources": [{"ref": "later-read", "evidence": "Additional consumer evidence"}]}, {"item": 2})["item"]["lifecycle"] == "closed"


def test_main_lines_are_actor_scoped_and_counts_are_opt_in(api):
    a = ok(api, "main.set", {"title": "Reader contract", "done_when": "Probe passes"}, {"main": 0})
    b = ok(api, "main.set", {"title": "Writer contract"}, {"main": 0}, actor="actor-b")
    assert a["main"]["id"] != b["main"]["id"]
    assert ok(api, "main.get")["main"]["title"] == "Reader contract"
    assert ok(api, "main.get", actor="actor-b")["main"]["title"] == "Writer contract"
    assert api("main.set", {"next_step": "stale"}, {"main": 0})[0] == 3
    create(api)
    assert "counts" not in ok(api, "list")
    assert ok(api, "list", {"include_counts": True})["counts"] == {"work": 1, "owner_pending": 0, "later": 0}


def test_child_writes_and_shared_failure_never_fall_back(api, home, monkeypatch):
    assert api("intake", finding(), role="child")[0] == 4
    assert not (home / "sample.db").exists()
    monkeypatch.setenv("I_HAVE_OCD_CHILD", "1")
    assert api("main.set", {"title": "child"}, {"main": 0})[0] == 4
    monkeypatch.delenv("I_HAVE_OCD_CHILD")
    monkeypatch.setenv("I_HAVE_OCD_BACKEND", "shared")
    code, result = api("intake", finding())
    assert code == 1 and result["error"]["code"] == "UNAVAILABLE"
    assert not (home / "sample.db").exists()
    monkeypatch.delenv("I_HAVE_OCD_BACKEND")
    item = create(api)
    monkeypatch.setenv("I_HAVE_OCD_BACKEND", "shared")
    assert api("list")[0] == 1
    monkeypatch.delenv("I_HAVE_OCD_BACKEND")
    assert shown(api, item)["version"] == 1


def test_markdown_deletion_is_divergence_never_closure(api, home):
    item = create(api)
    ok(api, "render")
    view = home / "sample.md"
    edited = "\n".join(line for line in view.read_text().splitlines() if item["id"] not in line) + "\n"
    view.write_text(edited)
    code, result = api("list")
    assert code == 3 and result["error"]["reason"] == "VIEW_DIVERGED" and result["committed"] is False
    assert api("render")[0] == 3
    with sqlite3.connect(home / "sample.db") as db:
        stored = json.loads(db.execute("SELECT data FROM concerns").fetchone()[0])
        assert stored["lifecycle"] == "active" and stored["version"] == 1
    assert view.read_text() == edited


def test_render_updates_explicitly_and_reads_leave_a_stale_projection_alone(api, home):
    item = create(api)
    ok(api, "render")
    before = (home / "sample.md").read_bytes()
    create(api, 2)
    assert len(ok(api, "list")["items"]) == 2
    assert (home / "sample.md").read_bytes() == before
    ok(api, "render")
    assert (home / "sample.md").read_bytes() != before
    assert item["id"] in (home / "sample.md").read_text()


def test_unavailable_missing_damaged_and_locked_are_not_empty(api, home, ocd, monkeypatch):
    assert api("list")[0] == 1
    assert not (home / "sample.db").exists()
    (home / "sample.db").write_bytes(b"not a database" * 100)
    assert api("list")[0] == 1
    (home / "sample.db").unlink()
    item = create(api)
    monkeypatch.setattr(ocd, "BUSY_SECONDS", 0.01)
    with sqlite3.connect(home / "sample.db") as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert api("intake", finding(2))[0] == 1
        assert shown(api, item)["version"] == 1
        writer.rollback()


def test_urgent_receipts_keep_first_notification_and_do_not_wait_for_review(api):
    item = create(api)
    item = ok(api, "urgent", {"item_id": item["id"], "action": "raise", "facts": "Probe detected lost writes", "pending_verification": "Impact not measured",
             "consequence": "Records may be missing", "responder": "actor-a"}, {"item": 1})["item"]
    assert item["urgency"]["first_notification"] is None
    item = ok(api, "urgent", {"item_id": item["id"], "action": "notified", "notification_ref": "outward-reply-1"}, {"item": 2})["item"]
    item = ok(api, "urgent", {"item_id": item["id"], "action": "notified", "notification_ref": "outward-reply-2"}, {"item": 3})["item"]
    assert item["urgency"]["first_notification"]["ref"] == "outward-reply-1"
    assert item["decisions"] == []


def test_export_import_preserves_decisions_history_and_fences_leases(api, home, ocd, capsys):
    result = propose(api, create(api))
    lease = take(api, result["item"])
    export = ok(api, "export", {"include_history": True})
    assert lease["token"] not in json.dumps(export) and "token_hash" not in json.dumps(export)
    source_events = len(export["snapshot"]["tables"]["events"])
    target = ocd.Store("restored", "actor-a", home=home)
    request = {"contract": ocd.CONTRACT, "op": "import.apply", "request_id": "restore-1", "actor": {"id": "actor-a"},
               "expected": {"revision": 0}, "input": export}
    try:
        restored = target.execute(request)
        assert restored["result"]["imported"] and restored["store_id"] == export["snapshot"]["store_id"]
        target.close()
        assert target.execute(request)["replayed"]
        target.close()
        target.open()
        assert len(target.history()) == source_events + 1
        assert target.public(target.resolve(result["item"]["id"]))["needs_owner"]
        assert target.get("leases", lease["id"])["fence"] > lease["fence"]
    finally:
        target.close()


def test_snapshot_missing_decision_link_is_rejected_without_partial_import(api, home, ocd):
    propose(api, create(api))
    export = ok(api, "export", {"include_history": True})
    export["snapshot"]["tables"]["decision_links"] = []
    export["digest"] = ocd.digest(export["snapshot"])
    target = ocd.Store("restored", "actor-a", home=home)
    try:
        with pytest.raises(ocd.Invalid, match="UNLINKED_DECISION"):
            target.execute({"contract": ocd.CONTRACT, "op": "import.apply", "request_id": "restore", "actor": {"id": "actor-a"},
                            "expected": {"revision": 0}, "input": export})
        assert target.rows("concerns") == []
    finally:
        target.close()


def test_import_plan_is_read_only_for_a_missing_target(api, home, ocd):
    create(api)
    export = ok(api, "export", {"include_history": True})
    target = ocd.Store("missing", "actor-a", home=home)
    result = target.execute({"contract": ocd.CONTRACT, "op": "import.plan", "input": export})
    assert result["result"]["ready"] and result["receipt"]["committed"] is False
    assert not target.path.exists()


def test_import_rejects_a_closed_concern_with_an_unanswered_choice(api, home, ocd):
    propose(api, create(api))
    export = ok(api, "export", {"include_history": True})
    row = export["snapshot"]["tables"]["concerns"][0]
    item = json.loads(row["data"])
    item.update(lifecycle="closed", closure={"kind": "resolved", "ref": "claim", "evidence": "A claim without an answer"})
    row["data"] = ocd.canonical(item)
    export["digest"] = ocd.digest(export["snapshot"])
    with pytest.raises(ocd.Invalid, match="CLOSED_WITH_UNANSWERED_DECISION"):
        ocd.validate_snapshot(export)


@pytest.mark.parametrize("command", ["park", "count", "decide", "done", "drop", "import-md"])
def test_obsolete_commands_require_client_upgrade(ocd, home, capsys, command):
    assert ocd.main([command]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["reason"] == "CLIENT_UPGRADE_REQUIRED"
    assert not (home / "sample.db").exists()
