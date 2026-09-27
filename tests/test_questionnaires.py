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
