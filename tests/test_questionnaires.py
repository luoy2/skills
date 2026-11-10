"""questionnaire-review: the deterministic parts around the model's labels.

Acceptance of a recommendation is the round's headline number, so extraction must
classify every answer shape Claude Code writes (a recommended option, another
option, the owner's own words, a multi-select list, a declined or dismissed
questionnaire), count a questionnaire copied into a resumed session once, and
respect the window. `leads` prints the owner's words and must mask secrets first.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "engineering" / "questionnaire-review" / "scripts" / "questionnaires.py"


@pytest.fixture(scope="module")
def qr():
    spec = importlib.util.spec_from_file_location("questionnaires_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ask(call_id, ts, questions):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "context before " + call_id},
                {"type": "tool_use", "id": call_id, "name": "AskUserQuestion", "input": {"questions": questions}}]}}


def _answer(call_id, ts, answers=None, error=None):
    block = {"type": "tool_result", "tool_use_id": call_id,
             "content": error or "Your questions have been answered.", "is_error": bool(error)}
    entry = {"type": "user", "timestamp": ts, "sessionId": "s1", "message": {"role": "user", "content": [block]}}
    if answers is not None:
        entry["toolUseResult"] = {"answers": answers, "annotations": {}}
    return entry


def _q(text, labels, multi=False):
    return {"question": text, "header": text[:8], "multiSelect": multi,
            "options": [{"label": label, "description": ""} for label in labels]}


WEBHOOK = "https://hooks.example.com/services/T0000/B0000/notARealTokenJustTheShapeOfOne0000"


def _write_log(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")


@pytest.fixture
def rows(qr, tmp_path):
    q1 = _q("Q1 ship now?", ["Ship now (Recommended)", "Wait"])
    q2 = _q("Q2 which store?", ["Table（推荐）", "File", "Both"])
    q3 = _q("Q3 who makes the hook?", ["You create it (Recommended)", "Skip"])
    q4 = _q("Q4 rotate?", ["Rotate (Recommended)", "Keep"])
    q5 = _q("Q5 dismissed?", ["Go (Recommended)", "Stop"])
    q6 = _q("Q6 which rows?", ["A (Recommended)", "B", "C"], multi=True)
    q0 = _q("Q0 too old", ["Old (Recommended)", "Older"])
    entries = [
        _ask("old", "2026-09-01T00:00:00Z", [q0]),
        _answer("old", "2026-09-01T00:01:00Z", {q0["question"]: "Older"}),
        _ask("a", "2026-09-20T00:00:00Z", [q1, q2]),
        _answer("a", "2026-09-20T00:01:00Z", {q1["question"]: "Ship now (Recommended)", q2["question"]: "Both"}),
        _ask("b", "2026-09-20T01:00:00Z", [q3]),
        _answer("b", "2026-09-20T01:00:30Z", {q3["question"]: f"make it yourself, the url is {WEBHOOK}"}),
        _ask("c", "2026-09-20T02:00:00Z", [q4]),
        _answer("c", "2026-09-20T02:00:05Z", error="The user doesn't want to proceed with this tool use."),
        {"type": "user", "timestamp": "2026-09-20T02:00:20Z", "message": {"role": "user", "content": "ask me later"}},
        _ask("d", "2026-09-20T03:00:00Z", [q5]),
        _answer("d", "2026-09-20T03:00:09Z", {q5["question"]: "[User dismissed — do not proceed, wait for next instruction]"}),
        _ask("e", "2026-09-20T04:00:00Z", [q6]),
        _answer("e", "2026-09-20T04:00:40Z", {q6["question"]: ["A (Recommended)", "C"]}),
    ]
    projects = tmp_path / "projects"
    _write_log(projects / "-proj" / "one.jsonl", entries)
    _write_log(projects / "-proj" / "resumed.jsonl", entries[2:4])  # a resumed session copies call "a"
    out = tmp_path / "rows.jsonl"
    qr.main(["extract", "--since", "2026-09-13T04:00:00Z", "--projects", str(projects), "--out", str(out)])
    return {r["id"]: r for r in qr.read_jsonl(out)}, out


def test_every_answer_shape_gets_its_kind_and_copies_count_once(rows):
    by_id, _ = rows
    assert sorted(by_id) == ["a#0", "a#1", "b#0", "c#0", "d#0", "e#0"]
    assert by_id["a#0"]["kind"] == "accepted" and by_id["a#0"]["recommended"] == [0]
    assert by_id["a#1"]["kind"] == "changed" and by_id["a#1"]["picked"] == [2]
    assert by_id["b#0"]["kind"] == "own" and "make it yourself" in by_id["b#0"]["own_text"]
    assert by_id["c#0"]["kind"] == "declined" and by_id["c#0"]["after"] == "ask me later"
    assert by_id["d#0"]["kind"] == "declined"
    assert by_id["e#0"]["kind"] == "multi" and by_id["e#0"]["picked"] == [0, 2]


def test_answer_time_is_kept_only_for_single_question_questionnaires(rows):
    by_id, _ = rows
    assert by_id["a#0"]["seconds"] is None
    assert by_id["b#0"]["seconds"] == 30


def test_stats_grade_only_single_choice_answers_with_a_recommendation(qr, rows, capsys):
    _, path = rows
    qr.main(["stats", "--rows", str(path)])
    out = capsys.readouterr().out
    assert "questionnaires 5, questions 6" in out
    assert "answered: 3: accepted 1 (33.3%) / changed 1 (33.3%) / own 1 (33.3%)" in out
    assert "by option position (0 = first): {2: 1}" in out


def test_leads_print_overrides_and_mask_secrets(qr, rows, capsys):
    _, path = rows
    qr.main(["leads", "--rows", str(path)])
    out = capsys.readouterr().out
    assert "Both" in out and "make it yourself" in out and "ask me later" in out
    assert WEBHOOK not in out and "[redacted]" in out
    assert "Q1 ship now?" not in out  # an accepted recommendation is not a lead


def test_labels_line_up_with_the_ids_asked_and_a_rerun_skips_labelled(qr, rows, monkeypatch, tmp_path):
    _, path = rows
    calls = []

    def fake(model, prompt, timeout):
        items = json.loads(prompt[prompt.index("["):])
        calls.append([i["id"] for i in items])
        return {"items": [{"id": i["id"], "rec_action": "act_now", "chosen_action": "act_now", "theme": "none", "note": ""}
                          for i in items] + [{"id": "not-asked", "rec_action": "other", "chosen_action": "other",
                                              "theme": "none", "note": ""}]}, 0.01

    monkeypatch.setattr(qr, "run_claude", fake)
    labels = tmp_path / "labels.jsonl"
    qr.main(["classify", "--rows", str(path), "--labels", str(labels), "--batch", "10", "--parallel", "1"])
    qr.main(["classify", "--rows", str(path), "--labels", str(labels), "--batch", "10", "--parallel", "1"])
    assert calls == [["a#0", "a#1", "b#0"]]
    assert sorted(label["id"] for label in qr.read_jsonl(labels)) == ["a#0", "a#1", "b#0"]


def test_a_failed_batch_counts_as_unlabelled_and_fails_the_run(qr, rows, monkeypatch, tmp_path, capsys):
    _, path = rows

    def unavailable(model, prompt, timeout):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(qr, "run_claude", unavailable)
    labels = tmp_path / "labels.jsonl"
    with pytest.raises(SystemExit) as stopped:
        qr.main(["classify", "--rows", str(path), "--labels", str(labels), "--batch", "2", "--parallel", "1"])
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert "labelled 0 of 3; cost $0.00; unlabelled 3 (2 of 2 batches failed)" in captured.out
    assert "batch failed: model unavailable" in captured.err
    assert qr.read_jsonl(labels) == []


def test_leads_print_dates_in_the_named_time_zone(qr, rows, capsys):
    _, path = rows
    qr.main(["leads", "--rows", str(path), "--tz", "America/New_York"])
    out = capsys.readouterr().out
    assert "[09-19 20:00]" in out and "[09-19 21:00]" in out  # asked 00:00Z and 01:00Z on 09-20


def test_a_zone_this_system_cannot_load_prints_utc_and_says_so(qr, rows, capsys):
    _, path = rows
    qr.main(["leads", "--rows", str(path), "--tz", "Not/AZone"])
    captured = capsys.readouterr()
    assert "[09-20 00:00Z]" in captured.out
    assert "time zone Not/AZone unavailable here" in captured.err


def test_a_hook_block_or_an_unreturned_call_is_not_the_owners_decline(qr, tmp_path, capsys):
    q = _q("Q7 go?", ["Go (Recommended)", "Stop"])
    entries = [
        _ask("h", "2026-09-21T00:00:00Z", [q, _q("Q8 also?", ["Yes (Recommended)", "No"])]),
        _answer("h", "2026-09-21T00:00:01Z", error="PreToolUse:AskUserQuestion hook error: [bash gate.sh]: "
                "route this question to the coordinator"),
        {"type": "user", "timestamp": "2026-09-21T00:00:30Z", "message": {"role": "user", "content": "carry on"}},
        _ask("i", "2026-09-21T01:00:00Z", [q]),
        _answer("i", "2026-09-21T01:00:02Z", error="[Tool call interrupted: the session ended before this call's "
                "result was recorded]"),
        _ask("j", "2026-09-21T02:00:00Z", [q]),
        _answer("j", "2026-09-21T02:00:03Z", error="Tool permission request failed: connection reset"),
        _ask("k", "2026-09-21T03:00:00Z", [q]),
        _answer("k", "2026-09-21T03:00:04Z", error="The user doesn't want to proceed with this tool use. "
                "The tool use was rejected."),
    ]
    projects = tmp_path / "projects"
    _write_log(projects / "-proj" / "one.jsonl", entries)
    out = tmp_path / "rows.jsonl"
    qr.main(["extract", "--since", "2026-09-13T04:00:00Z", "--projects", str(projects), "--out", str(out)])
    by_id = {r["id"]: r for r in qr.read_jsonl(out)}
    assert [by_id[i]["kind"] for i in ("h#0", "h#1", "i#0", "j#0", "k#0")] == [
        "hook_blocked", "hook_blocked", "missing", "error", "declined"]
    capsys.readouterr()
    qr.main(["stats", "--rows", str(out)])
    stats = capsys.readouterr().out
    assert "declined questionnaires: 1" in stats and "hook-blocked questionnaires: 1" in stats
    assert "answered: 0:" in stats  # none of them enters the acceptance rate
    qr.main(["leads", "--rows", str(out)])
    leads = capsys.readouterr().out
    assert "Q7 go?" in leads and leads.count("declined; then wrote") == 1  # only the owner's own refusal
    assert "carry on" not in leads  # text after a hook block is not read as a reason to decline


@pytest.mark.parametrize("count", [1, 4])
def test_cached_results_do_not_consume_official_calls(qr, tmp_path, count):
    questions = [_q(f"Question {i}?", ["Go (Recommended)", "Stop"]) for i in range(count)]
    call = _ask("cached", "2026-09-21T00:00:00Z", questions)
    cached = _answer("cached", None, {q["question"]: "Go (Recommended)" for q in questions})
    cached["type"] = "api-request-blob"
    cached.pop("timestamp")
    answer = _answer("cached", "2026-09-21T00:00:15Z", {q["question"]: "Stop" for q in questions})
    root = tmp_path / "projects"
    _write_log(root / "proj" / "s.jsonl", [call, cached, answer])
    out = tmp_path / "rows.jsonl"
    qr.main(["extract", "--since", "2026-09-20T00:00:00Z", "--projects", str(root), "--out", str(out)])
    rows = qr.read_jsonl(out)
    assert len(rows) == count and all(r["kind"] == "changed" and r["answer"] == "Stop" for r in rows)
    assert all(r["sources"][0]["call_line"] == 1 and r["sources"][0]["result_line"] == 3 for r in rows)
    report = json.loads(Path(str(out) + ".coverage.json").read_text())
    assert any(d["code"] == "ignored_nonofficial_event" and d["line"] == 2 for d in report["diagnostics"])


def test_nonofficial_calls_never_register_a_questionnaire(qr, tmp_path):
    q = _q("Cached call?", ["Go (Recommended)", "Stop"])
    cached = _ask("cache-only", "2026-09-21T00:00:00Z", [q])
    cached["type"] = "api-request-blob"
    path = tmp_path / "cached.jsonl"
    _write_log(path, [cached, _answer("cache-only", "2026-09-21T00:00:02Z", {q["question"]: "Stop"})])
    assert list(qr.calls_in(path)) == []


@pytest.mark.parametrize("value", [None, "not-a-time", "2026-09-21T00:00:00"])
def test_unavailable_result_timestamp_keeps_answer_without_fabricating_duration(qr, tmp_path, value):
    q = _q("Keep answer?", ["Go (Recommended)", "Stop"])
    root = tmp_path / "projects"
    answer = _answer("time", value, {q["question"]: "Stop"})
    if value is None:
        answer.pop("timestamp")
    _write_log(root / "p" / "s.jsonl", [_ask("time", "2026-09-21T00:00:00Z", [q]), answer])
    out = tmp_path / "rows.jsonl"
    qr.main(["extract", "--since", "2026-09-20T00:00:00Z", "--projects", str(root), "--out", str(out)])
    row, = qr.read_jsonl(out)
    assert row["kind"] == "changed" and row["seconds"] is None
    report = json.loads(Path(str(out) + ".coverage.json").read_text())
    assert any(d["code"] == "unknown_answer_time" for d in report["diagnostics"])


def test_multiroot_copies_merge_provenance_and_conflicts_are_quarantined(qr, tmp_path, capsys):
    q = _q("Which copy?", ["Go (Recommended)", "Stop"])
    first, second = tmp_path / "a", tmp_path / "b"
    common = [_ask("same", "2026-09-21T00:00:00Z", [q]),
              _answer("same", "2026-09-21T00:00:03Z", {q["question"]: "Stop"})]
    conflicting = _ask("conflict", "2026-09-21T01:00:00Z", [q])
    _write_log(first / "p" / "s.jsonl", common + [conflicting,
               _answer("conflict", "2026-09-21T01:00:03Z", {q["question"]: "Stop"})])
    _write_log(second / "renamed-p" / "s-copy.jsonl", common + [conflicting,
               _answer("conflict", "2026-09-21T01:00:03Z", {q["question"]: "Go (Recommended)"})])
    outputs = []
    for n, roots in enumerate(([first, second], [second, first])):
        out = tmp_path / f"rows-{n}.jsonl"
        argv = ["extract", "--since", "2026-09-20T00:00:00Z", "--out", str(out)]
        for root in roots:
            argv += ["--projects", str(root)]
        with pytest.raises(SystemExit) as stopped:
            qr.main(argv)
        assert stopped.value.code == 1
        rows = qr.read_jsonl(out)
        row, = rows
        assert row["id"] == "same#0" and len(row["sources"]) == 2
        report = json.loads(Path(str(out) + ".coverage.json").read_text())
        assert report["duplicate_calls"] == 1 and report["conflicting_calls"] == 1
        assert len(report["roots"]) == 2 and all(r["status"] == "read" for r in report["roots"])
        assert any(d["code"] == "conflicting_copy" and d["call"] == "conflict" for d in report["diagnostics"])
        outputs.append(rows)
        qr.main(["stats", "--rows", str(out)])
        qr.main(["leads", "--rows", str(out)])
    assert outputs[0] == outputs[1]
    assert "conflicting calls 1" in capsys.readouterr().out


def test_missing_root_is_not_a_successful_empty_scan(qr, tmp_path):
    out = tmp_path / "rows.jsonl"
    with pytest.raises(SystemExit) as stopped:
        qr.main(["extract", "--since", "2026-09-20T00:00:00Z", "--projects", str(tmp_path / "absent"), "--out", str(out)])
    assert stopped.value.code == 1
    report = json.loads(Path(str(out) + ".coverage.json").read_text())
    assert report["roots"][0]["status"] == "missing"
    assert qr.read_jsonl(out) == []


def test_unreadable_log_and_invalid_call_timestamp_are_reported(qr, tmp_path, monkeypatch):
    q = _q("Bad clock?", ["Go (Recommended)", "Stop"])
    root = tmp_path / "projects"
    bad, unread = root / "p" / "bad.jsonl", root / "p" / "unread.jsonl"
    _write_log(bad, [_ask("bad-clock", None, [q])])
    _write_log(unread, [])
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path == unread:
            raise PermissionError("test unreadable source")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    out = tmp_path / "rows.jsonl"
    with pytest.raises(SystemExit) as stopped:
        qr.main(["extract", "--since", "2026-09-20T00:00:00Z", "--projects", str(root), "--out", str(out)])
    assert stopped.value.code == 1
    report = json.loads(Path(str(out) + ".coverage.json").read_text())
    assert {d["code"] for d in report["diagnostics"]} >= {"unreadable_source", "invalid_call_timestamp"}
    assert next(d for d in report["diagnostics"] if d["code"] == "unreadable_source")["path"] == str(unread)
    assert qr.read_jsonl(out) == []


def test_multiroot_keeps_half_open_window_and_default_root(qr, tmp_path, monkeypatch):
    q = _q("Window?", ["Go (Recommended)", "Stop"])
    root = tmp_path / ".claude" / "projects"
    _write_log(root / "p" / "s.jsonl", [
        _ask("inside", "2026-09-21T00:00:00Z", [q]),
        _answer("inside", "2026-09-21T00:00:03Z", {q["question"]: "Stop"}),
        _ask("boundary", "2026-09-22T00:00:00Z", [q])])
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "rows.jsonl"
    qr.main(["extract", "--since", "2026-09-21T00:00:00Z", "--until", "2026-09-22T00:00:00Z", "--out", str(out)])
    assert [r["id"] for r in qr.read_jsonl(out)] == ["inside#0"]
    report = json.loads(Path(str(out) + ".coverage.json").read_text())
    assert report["roots"][0]["root"] == str(root)


def test_changed_answer_does_not_reuse_a_label_by_id_alone(qr, rows, tmp_path, monkeypatch, capsys):
    _, path = rows
    calls = []
    def fake(model, prompt, timeout):
        items = json.loads(prompt[prompt.index("["):])
        calls.append([i["id"] for i in items])
        return {"items": [{"id": i["id"], "rec_action": "act_now", "chosen_action": "act_now", "theme": "none", "note": ""}
                          for i in items]}, 0
    monkeypatch.setattr(qr, "run_claude", fake)
    labels = tmp_path / "labels.jsonl"
    argv = ["classify", "--rows", str(path), "--labels", str(labels), "--parallel", "1"]
    qr.main(argv)
    changed = qr.read_jsonl(path)
    changed[0].update(answer="Wait", kind="changed", picked=[1])
    path.write_text("".join(json.dumps(r) + "\n" for r in changed))
    capsys.readouterr()
    qr.main(["stats", "--rows", str(path), "--labels", str(labels)])
    assert "labelled 2 of 3" in capsys.readouterr().out
    qr.main(argv)
    assert calls == [["a#0", "a#1", "b#0"], ["a#0"]]
