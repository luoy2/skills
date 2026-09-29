"""i-have-ocd store: the contract that lets several sessions on one machine share a queue.

On 2026-09-29 a session asked the owner about an item another session had removed 18 minutes
earlier, from a read made before the removal: every session rewrote one Markdown file whole,
items had no identity, and closing left no receipt. These cases hold the helper to what fixes
that: ids that never change or repeat, two concerns on one ticket kept apart, versioned and
retry-safe changes, leases a stale holder cannot write through, receipts for every close,
closed items that stay closed, a store that fails loudly instead of reading as empty, the first
import from the real store's shape, and hand edits of the view from sessions still on the old
skill. Cases that start a process live in tests/integration/test_ocd_store_integration.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "engineering" / "i-have-ocd" / "scripts" / "ocd.py"

# Shaped like a real hand-written store (long main-line bullets, a key with a parenthesis, Chinese
# text, two concerns on one ticket, a line written twice); the content is invented.
LONG_NEXT_STEP = ("#401/#405/#445 merged + #447 fix (0d692e1, on the staging branch; CI run 1234 pending). "
                  "Refund thresholds rebased to the net amount ex-fees, owner-approved; the 09:14 job carries the "
                  "new sha. " * 12).strip()
LEGACY = f"""# shop — i-have-ocd store (this machine only until the shared focus server ships)

## Main line
- title: Checkout v2 (EU storefront) onto the new payments tenant — M-3a
- done-when: roadmap v1.4 M-3 card, gate G3a (docs/milestones/m-3-payments.md); rulings in #412 comment 9001
- next step (2026-09-28 22:50 ET): {LONG_NEXT_STEP}
- open main-line decision (owner, 09-28): #418 — add a second API user or keep one service user. Waiting for reply.
- ruled 2026-09-27 (questionnaire): API credentials come from the secret store (field by field, hash-checked)
- 3a input: verify the webhook's real retry cadence before designing the 3a coordination
- session: checkout main-line session s-17 (mesh s-abc123), took over 2026-09-27 19:36 ET
- set: 2026-09-27 by owner ("我其实想现在先管这个结账的事情")

## Parked
- #433（固定批量受预算约束）两个待裁点：机主 09-29 让问设计负责人，消息已发（对方离线排队）。
- [open] after the docs rename PR lands, add two docs to the alias scan in tests/test_aliases.py (#440).
- #445 判据 3 旁支：订单 `_revoke` 对 PRE_SUBMIT 的出场直接转 EXPIRED，不开 exit_unfilled 事项。
- #445 告警缺口：硬约束撤销的出场同样没人被叫，要不要补由机主裁。
- 包装器用了 jobs.yaml 里没有的作业名 nightly_recon（receipt_wrap --job）· 源：impl-7 #451 汇报 · 机主无需裁
- 包装器用了 jobs.yaml 里没有的作业名 nightly_recon（receipt_wrap --job）· 源：impl-7 #451 汇报 · 机主无需裁
- job_logs.exit_code 列在信息级退出时写 0；真实码只在 statistics.exit_code
"""
LEGACY_PARKED = [line[2:] for line in LEGACY.split("## Parked\n")[1].splitlines() if line.startswith("- ")]


@pytest.fixture(scope="module")
def ocd():
    spec = importlib.util.spec_from_file_location("ocd_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("I_HAVE_OCD_HOME", str(tmp_path))
    monkeypatch.setenv("I_HAVE_OCD_PROJECT", "t")
    monkeypatch.setenv("I_HAVE_OCD_BY", "s1")
    return tmp_path


@pytest.fixture
def cli(ocd, home, capsys):
    """Run one command in this process: (exit code, parsed JSON or the printed text)."""
    def run(*argv):
        code = ocd.main([str(a) for a in argv])
        out = capsys.readouterr().out
        return code, json.loads(out) if out.startswith("{") else out.strip()
    return run


def park(cli, summary, request_id, *extra):
    code, out = cli("park", summary, "--request-id", request_id, *extra)
    assert code == 0, out
    return out["item"]


def test_ids_are_stable_and_never_reused(cli):
    first, second, third = (park(cli, f"item {n}", f"r{n}") for n in (1, 2, 3))
    assert [first["ref"], second["ref"], third["ref"]] == ["P1", "P2", "P3"]
    assert cli("done", "P3", "--kind", "resolved", "--ref", "fixed in abc123", "--expected-version", 1,
               "--request-id", "d3")[0] == 0
    assert park(cli, "item 4", "r4")["ref"] == "P4"
    code, listed = cli("list")
    assert [(i["ref"], i["summary"]) for i in listed["items"]] == [("P1", "item 1"), ("P2", "item 2"), ("P4", "item 4")]


def test_two_concerns_on_one_ticket_stay_two_items(cli):
    a = park(cli, "#445 PRE_SUBMIT exit expires without an alert", "a", "--tracking", "#445")
    b = park(cli, "#445 a hard-constraint revoke pages nobody", "b", "--tracking", "#445", "--needs-owner")
    assert a["id"] != b["id"]
    code, listed = cli("list")
    assert listed["counts"]["pending"] == 2 and listed["counts"]["needs_owner"] == 1
    assert cli("done", "P1", "--kind", "ticket", "--ref", "#445 comment 1", "--expected-version", 1,
               "--request-id", "d1")[0] == 0
    assert [i["ref"] for i in cli("list")[1]["items"]] == ["P2"]


def test_add_source_keeps_the_id(cli):
    park(cli, "flaky test_x", "p", "--source", "run 1")
    code, out = cli("add-source", "P1", "--source", "run 2", "--tracking", "#7", "--expected-version", 1,
                    "--request-id", "s1")
    assert code == 0
    assert (out["item"]["ref"], out["item"]["version"]) == ("P1", 2)
    assert out["item"]["source_refs"] == ["run 1", "run 2"] and out["item"]["tracking_refs"] == ["#7"]
    code, out = cli("add-source", "P1", "--source", "run 2", "--expected-version", 2, "--request-id", "s2")
    assert out["unchanged"] is True and out["item"]["version"] == 2
    assert cli("count") == (0, "1")


def test_a_retried_request_returns_the_original_result_and_a_reused_id_conflicts(cli):
    first = park(cli, "one", "r1")
    code, again = cli("park", "one", "--request-id", "r1")
    assert code == 0 and again["replayed"] is True and again["item"]["id"] == first["id"]
    assert cli("count") == (0, "1")
    done = ("done", "P1", "--kind", "pr", "--ref", "#41", "--expected-version", 1, "--request-id", "d1")
    assert cli(*done)[0] == 0
    code, retried = cli(*done)  # the version has moved on; the retry is still the same request
    assert code == 0 and retried["replayed"] is True and retried["item"]["status"] == "done"
    code, conflict = cli("done", "P1", "--kind", "pr", "--ref", "#42", "--expected-version", 1, "--request-id", "d1")
    assert code == 3 and conflict["error"] == "conflict"
    assert conflict["original"]["item"]["receipt"] == {"kind": "pr", "ref": "#41"}
    assert conflict["current"]["status"] == "done"


def test_a_stale_version_conflicts_and_returns_the_current_state(cli):
    park(cli, "one", "r1")
    assert cli("add-source", "P1", "--source", "a", "--expected-version", 1, "--request-id", "a")[0] == 0
    code, out = cli("decide", "P1", "--ref", "questionnaire Q3: do it", "--expected-version", 1, "--request-id", "q")
    assert code == 3
    assert out["current"]["version"] == 2 and out["current"]["source_refs"] == ["a"]
    assert out["current"]["decision_ref"] is None


def test_a_lease_expires_can_be_taken_again_and_the_stale_holder_is_refused(ocd, cli, monkeypatch):
    park(cli, "one", "r1")
    code, taken = cli("take", "P1", "--expected-version", 1, "--request-id", "t1", "--ttl-minutes", 30)
    assert code == 0 and taken["item"]["claim"]["holder"] == "s1" and "claim_token" not in taken["item"]
    token = taken["token"]
    code, out = cli("take", "P1", "--expected-version", 2, "--request-id", "t2", "--by", "s2")
    assert code == 3 and "held by s1" in out["message"]
    code, out = cli("done", "P1", "--kind", "resolved", "--ref", "x", "--expected-version", 2, "--request-id", "d0",
                    "--by", "s2")
    assert code == 3 and "held by s1" in out["message"]
    listed = cli("list")[1]
    assert listed["counts"]["taken"] == 1 and listed["items"][0]["claim"]["expired"] is False

    later = ocd.utcnow() + timedelta(hours=1)
    monkeypatch.setattr(ocd, "utcnow", lambda: later)
    assert cli("list")[1]["items"][0]["claim"]["expired"] is True
    code, retaken = cli("take", "P1", "--expected-version", 2, "--request-id", "t3", "--by", "s2")
    assert code == 0 and retaken["item"]["claim"]["holder"] == "s2" and retaken["token"] != token
    code, out = cli("done", "P1", "--kind", "resolved", "--ref", "x", "--expected-version", 3, "--request-id", "d1",
                    "--token", token)
    assert code == 3 and "stale holder" in out["message"]
    code, out = cli("done", "P1", "--kind", "resolved", "--ref", "x", "--expected-version", 3, "--request-id", "d2",
                    "--token", retaken["token"], "--by", "s2")
    assert code == 0 and out["item"]["closed_by"] == "s2"


@pytest.mark.parametrize("receipt", [
    (), ("--kind", "ticket"), ("--kind", "ticket", "--ref", "  "), ("--kind", "ack", "--ref", "msg 1"),
    ("--kind", "legacy-hand-removed", "--ref", "t.md"),
])
def test_done_without_a_valid_receipt_is_refused(cli, receipt):
    park(cli, "one", "r1")
    code, out = cli("done", "P1", *receipt, "--expected-version", 1, "--request-id", "d")
    assert code == 2 and out["error"] == "invalid"
    assert cli("count") == (0, "1")


def test_drop_needs_the_owners_decision(cli):
    park(cli, "one", "r1")
    assert cli("drop", "P1", "--expected-version", 1, "--request-id", "x")[0] == 2
    code, out = cli("drop", "P1", "--decision", "questionnaire 09-29 Q2: drop", "--expected-version", 1,
                    "--request-id", "y")
    assert code == 0 and out["item"]["status"] == "dropped"
    assert out["item"]["decision_ref"] == out["item"]["receipt"]["ref"] == "questionnaire 09-29 Q2: drop"


def test_a_ruling_keeps_the_item_pending(cli):
    park(cli, "one", "r1", "--needs-owner")
    code, out = cli("decide", "P1", "--ref", "questionnaire 09-29 Q1: do it", "--expected-version", 1,
                    "--request-id", "q")
    assert code == 0
    assert (out["item"]["status"], out["item"]["needs_owner"]) == ("open", False)
    assert out["item"]["decision_ref"] == "questionnaire 09-29 Q1: do it" and out["item"]["receipt"] is None
    listed = cli("list")[1]
    assert listed["counts"]["pending"] == 1 and listed["counts"]["needs_owner"] == 0
    assert cli("count") == (0, "1")


def test_a_closed_item_is_never_reopened(cli):
    park(cli, "one", "r1")
    assert cli("done", "P1", "--kind", "scheduled", "--ref", "job 7 at 09:14", "--expected-version", 1,
               "--request-id", "d")[0] == 0
    for argv in (("take", "P1", "--expected-version", 2, "--request-id", "a"),
                 ("decide", "P1", "--ref", "x", "--expected-version", 2, "--request-id", "b"),
                 ("add-source", "P1", "--source", "x", "--expected-version", 2, "--request-id", "c"),
                 ("drop", "P1", "--decision", "x", "--expected-version", 2, "--request-id", "e")):
        code, out = cli(*argv)
        assert code == 3 and "never reopened" in out["message"] and out["current"]["status"] == "done"
    recurrence = park(cli, "one, again", "r2", "--source", "recurrence of P1")
    assert recurrence["ref"] == "P2"


def test_count_on_a_missing_or_damaged_store_is_a_question_mark(cli, home):
    assert cli("count") == (1, "?")
    code, out = cli("list")
    assert code == 1 and out["error"] == "unavailable" and "no store" in out["message"]
    assert not (home / "t.db").exists()  # a read never creates the store
    (home / "t.db").write_bytes(b"this is not a database, just bytes" * 200)
    assert cli("count") == (1, "?")
    assert cli("list")[0] == 1
    (home / "t.db").unlink()
    other = sqlite3.connect(home / "t.db")
    other.execute("CREATE TABLE something_else (x)")
    other.commit()
    other.close()
    assert cli("count") == (1, "?")


def test_a_locked_store_fails_explicitly_and_reads_go_on(ocd, cli, home, monkeypatch):
    park(cli, "one", "r1")
    monkeypatch.setattr(ocd, "BUSY_SECONDS", 0.2)
    writer = sqlite3.connect(home / "t.db", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        code, out = cli("park", "two", "--request-id", "r2")
        assert code == 1 and "locked" in out["message"]
        assert cli("count") == (0, "1")
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_the_main_line_changes_only_from_the_version_read(cli):
    assert cli("main", "get")[0] == 1  # no store yet: `main set` creates it
    code, out = cli("main", "set", "--title", "MS-6a", "--done-when", "gate G6a", "--next-step", "run the probe",
                    "--set-by", "owner 09-27", "--expected-version", 0, "--request-id", "m1")
    assert code == 0 and out["main"]["version"] == 1
    code, out = cli("main", "set", "--next-step", "stale", "--expected-version", 0, "--request-id", "m2")
    assert code == 3 and out["current"]["next_step"] == "run the probe"
    code, out = cli("main", "set", "--paused-for", "owner's detour", "--expected-version", 1, "--request-id", "m3")
    assert code == 0 and (out["main"]["title"], out["main"]["paused_for"]) == ("MS-6a", "owner's detour")
    got = cli("main", "get")[1]
    assert got["version"] == 2 and got["main"]["next_step"] == "run the probe"


def test_import_md_keeps_every_line_verbatim_and_a_rerun_adds_nothing(cli, home):
    view = home / "t.md"
    view.write_text(LEGACY, encoding="utf-8")
    code, out = cli("park", "new", "--request-id", "p")
    assert code == 1 and "import-md" in out["message"]
    assert cli("count") == (1, "?")

    code, out = cli("import-md", view)
    assert code == 0, out
    assert len(out["imported"]) == len(LEGACY_PARKED) == 7 and out["main"] == "set"
    assert Path(out["backup"]).read_text(encoding="utf-8") == LEGACY
    items = cli("list")[1]["items"]
    assert [i["summary"] for i in items] == LEGACY_PARKED  # the duplicate line and both #445 lines stay apart
    assert all(i["source_refs"] == ["legacy t.md"] for i in items)
    main = cli("main", "get")[1]["main"]
    assert main["title"].startswith("Checkout v2") and main["next_step"] == f"(2026-09-28 22:50 ET) {LONG_NEXT_STEP}"
    assert main["set_by"] == '2026-09-27 by owner ("我其实想现在先管这个结账的事情")'
    assert [n.split(":")[0] for n in main["notes"]] == [
        "open main-line decision (owner, 09-28)", "ruled 2026-09-27 (questionnaire)", "3a input", "session"]

    copy = home / "vol-copy.md"
    copy.write_text(LEGACY, encoding="utf-8")
    code, again = cli("import-md", copy)
    assert code == 0 and again["imported"] == [] and len(again["already"]) == 7 and again["main"] == "unchanged"
    assert cli("count") == (0, "7")
    code, out = cli("import-md", view)
    assert code == 2 and "generated" in out["message"]


def test_a_hand_edit_of_the_view_is_reconciled_by_the_next_command(cli, home):
    for n in (1, 2, 3):
        park(cli, f"item {n}", f"r{n}", "--source", f"s{n}")
    code, rendered = cli("render")
    view = home / "t.md"
    assert code == 0 and rendered["hash"] == hashlib.sha256(view.read_bytes()).hexdigest()
    text = view.read_text(encoding="utf-8")
    assert text.startswith("<!-- GENERATED by i-have-ocd") and "ocd.py" in text.splitlines()[0]

    lines = text.splitlines()
    lines = [line + "; also seen in run 9" if line.startswith("- P1 ·") else line
             for line in lines if not line.startswith("- P2 ·")]
    lines.append("- a finding an old-skill session wrote by hand")
    lines.insert(lines.index("## Main line") + 1, "- title: set by hand")
    view.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code, out = cli("park", "item 4", "--request-id", "r4")
    assert code == 0
    assert out["reconciled"] == {"edited": ["P1"], "parked": ["P4"], "closed": ["P2"], "main": True}
    assert out["item"]["ref"] == "P5"
    p2 = cli("show", "P2")[1]["item"]
    assert p2["status"] == "done" and p2["receipt"] == {"kind": "legacy-hand-removed", "ref": "t.md"}
    assert cli("show", "P1")[1]["item"]["source_refs"] == ["s1", "hand-edited t.md: also seen in run 9"]
    assert cli("show", "P4")[1]["item"]["source_refs"] == ["hand-edited t.md"]
    assert cli("main", "get")[1]["main"]["title"] == "set by hand"
    assert [i["ref"] for i in cli("list")[1]["items"]] == ["P1", "P3", "P4", "P5"]
    assert "P2 ·" not in view.read_text(encoding="utf-8")
    assert "reconciled" not in cli("list")[1]  # taken in once


def test_a_stale_whole_file_write_neither_duplicates_nor_reopens(cli, home):
    """An old-skill session read the Markdown before the import and writes it back whole, minus the line it handled."""
    view = home / "t.md"
    view.write_text(LEGACY, encoding="utf-8")
    assert cli("import-md", view)[0] == 0
    assert cli("done", "P2", "--kind", "ticket", "--ref", "#440", "--expected-version", 1, "--request-id", "d")[0] == 0
    view.write_text(LEGACY.replace(f"- {LEGACY_PARKED[0]}\n", ""), encoding="utf-8")
    code, listed = cli("list")
    assert code == 0
    assert listed["reconciled"]["closed"] == ["P1"]
    assert listed["reconciled"]["ignored"] == ["P2: done; a closed item is never reopened"]
    assert "parked" not in listed["reconciled"] and "edited" not in listed["reconciled"]
    assert [i["ref"] for i in listed["items"]] == ["P3", "P4", "P5", "P6", "P7"]
    assert cli("show", "P1")[1]["item"]["receipt"]["kind"] == "legacy-hand-removed"
    assert cli("show", "P2")[1]["item"]["receipt"] == {"kind": "ticket", "ref": "#440"}


def test_a_view_without_its_parked_section_is_set_aside_not_emptied(cli, home):
    park(cli, "one", "r1")
    view = home / "t.md"
    view.write_text("oops\n", encoding="utf-8")
    code, listed = cli("list")
    assert code == 0 and [i["ref"] for i in listed["items"]] == ["P1"]
    assert Path(listed["reconciled"]["unreadable"]).read_text(encoding="utf-8") == "oops\n"
    assert "- P1 · one" in view.read_text(encoding="utf-8")
