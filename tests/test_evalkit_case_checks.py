"""agent-eval case validity: the checks that keep a case from measuring the wrong thing.

Every case in the first implementation batch passed the old gates (the judges failed the
negative, the reference passed the hidden tests) and still measured the wrong thing: a Trap
no answer was shown to pass, tests asserting sentences nobody could guess, names the plan never
disclosed. Each test below names the wrong conclusion its check prevents. The cases that build
a git repository are in tests/integration/test_evalkit_case_checks_integration.py.
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"


@pytest.fixture
def kit():
    spec = importlib.util.spec_from_file_location("evalkit_case_checks_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases(tmp_path, kit, monkeypatch, **extra):
    """A cases dir with one planning case P (with a positive) and one implementation case W."""
    cases = tmp_path / "cases"
    for cid, body in (("P", {"calibration_negative": "neg.md", "calibration_positive": "pos.md"}),
                      ("W", {"kind": "implement", "calibration_negative": "neg.md",
                             "hidden_tests": {"files": [], "paths": [], "expected": 3}})):
        (cases / cid).mkdir(parents=True)
        case = {"id": cid, "title": cid, "snapshot": "0" * 40, "background": ["b"], "owner_messages": ["m"],
                "trap": {"description": "d", "direction": "dir", "pass": "p"}, "rubric": [], "equivalents": "",
                **body, **extra.get(cid, {})}
        (cases / cid / "case.json").write_text(json.dumps(case), encoding="utf-8")
        (cases / cid / "neg.md").write_text("negative", encoding="utf-8")
        (cases / cid / "pos.md").write_text("positive", encoding="utf-8")
    (cases / "common.json").write_text(json.dumps({"rubric": [], "suffix": "s", "implement_suffix": "i"}),
                                       encoding="utf-8")
    monkeypatch.setattr(kit, "CASES", cases)
    monkeypatch.setattr(kit, "RESULTS", tmp_path / "results")
    return cases


# ------------------------------------------------------------------ case digest

def test_the_case_digest_is_stable_and_moves_with_any_case_file_or_the_shared_rubric(kit, tmp_path, monkeypatch):
    """A calibration of yesterday's hidden tests must not vouch for today's edited ones."""
    cases = _cases(tmp_path, kit, monkeypatch)
    _, loaded = kit.load_cases(["W"])
    first = kit.case_digest(loaded["W"])
    assert first == kit.case_digest(loaded["W"]) and len(first) == 12
    (cases / "W" / ".DS_Store").write_bytes(b"finder")
    assert kit.case_digest(loaded["W"]) == first
    (cases / "W" / "hidden").mkdir()
    (cases / "W" / "hidden" / "t.py.hidden").write_text("def test_x(): pass\n", encoding="utf-8")
    second = kit.case_digest(loaded["W"])
    assert second != first
    (cases / "common.json").write_text(json.dumps({"rubric": [{"id": "C1"}]}), encoding="utf-8")
    assert kit.case_digest(loaded["W"]) != second


# -------------------------------------------------------------- judge calibration

def _judge_by_answer(calls):
    """A judge that passes the positive and fails the negative, recording each call."""
    def call(judge, prompt, workdir, key, timeout):
        calls.append(judge["id"])
        good = "positive" in prompt.rsplit("=== Candidate final answer ===", 1)[1]
        return {"trap": {"direction": good, "pass": good, "evidence": "e"}, "items": []}, {}, 0.1
    return call


def _cal_args(batch="b1", cases=None):
    return types.SimpleNamespace(batch=batch, cases=cases, timeout=10)


def test_a_case_without_a_positive_fails_calibration_without_calling_a_judge(kit, tmp_path, monkeypatch):
    """No positive means nobody showed the Trap passable: models that all fail would look like a hard case."""
    _cases(tmp_path, kit, monkeypatch)
    monkeypatch.setattr(kit, "CFG", {"judges": [{"id": "j1"}, {"id": "j2"}], "prices": {}})
    monkeypatch.setattr(kit, "gateway_key", lambda: "k")
    calls = []
    monkeypatch.setattr(kit, "call_judge", _judge_by_answer(calls))
    assert kit.cmd_calibrate(_cal_args(cases=["W"])) == 1
    assert calls == []
    _, loaded = kit.load_cases(["W"])
    rec = kit.read_calibration("judges", "W", kit.case_digest(loaded["W"]))
    assert rec["ok"] is False and "calibration_positive" in rec["reason"]


def test_a_case_whose_judges_fail_the_negative_and_pass_the_positive_is_recorded_passing(kit, tmp_path, monkeypatch):
    _cases(tmp_path, kit, monkeypatch)
    monkeypatch.setattr(kit, "CFG", {"judges": [{"id": "j1"}, {"id": "j2"}], "prices": {}})
    monkeypatch.setattr(kit, "gateway_key", lambda: "k")
    calls = []
    monkeypatch.setattr(kit, "call_judge", _judge_by_answer(calls))
    assert kit.cmd_calibrate(_cal_args(cases=["P"])) == 0
    assert len(calls) == 4
    _, loaded = kit.load_cases(["P"])
    rec = kit.read_calibration("judges", "P", kit.case_digest(loaded["P"]))
    assert rec["ok"] is True
    assert rec["judges"]["j1"] == {"negative": {"direction": False, "pass": False, "ok": True},
                                   "positive": {"direction": True, "pass": True, "ok": True}}


# ------------------------------------------------------------------- run gate

class _PastTheGate(Exception):
    pass


def _run_args(cases, dry_run=False):
    return types.SimpleNamespace(batch="b1", cases=cases, modes=None, candidates=None, efforts=None, repeats=None,
                                 rerun=False, dry_run=dry_run, parallel=1, timeout=None, budget=None)


def _stop_at_launch(kit, monkeypatch):
    def stop():
        raise _PastTheGate
    monkeypatch.setattr(kit, "gateway_key", stop)
    monkeypatch.setattr(kit, "CFG", {"candidates": [{"id": "x", "efforts": ["high"]}], "modes": ["solo"],
                                     "implement": {"candidates": [{"id": "x", "efforts": ["high"]}], "modes": ["direct"]},
                                     "prices": {}, "judges": []})


def test_run_refuses_a_case_with_no_calibration_record_and_names_the_command(kit, tmp_path, monkeypatch):
    _cases(tmp_path, kit, monkeypatch)
    _stop_at_launch(kit, monkeypatch)
    with pytest.raises(kit.EvalError) as err:
        kit.cmd_run(_run_args(["P"]))
    _, loaded = kit.load_cases(["P"])
    assert kit.case_digest(loaded["P"]) in str(err.value)
    assert "calibrate --batch <batch> --cases P" in str(err.value)


def test_run_launches_on_passing_records_of_the_current_digest_only(kit, tmp_path, monkeypatch):
    cases = _cases(tmp_path, kit, monkeypatch)
    _stop_at_launch(kit, monkeypatch)
    _, loaded = kit.load_cases(["P"])
    kit.write_calibration("judges", "P", kit.case_digest(loaded["P"]), {"ok": True})
    with pytest.raises(_PastTheGate):
        kit.cmd_run(_run_args(["P"]))
    (cases / "P" / "pos.md").write_text("an edited positive", encoding="utf-8")
    with pytest.raises(kit.EvalError, match="judges calibration missing"):
        kit.cmd_run(_run_args(["P"]))


def test_an_implementation_case_needs_its_tests_calibrated_as_well(kit, tmp_path, monkeypatch):
    _cases(tmp_path, kit, monkeypatch)
    _stop_at_launch(kit, monkeypatch)
    _, loaded = kit.load_cases(["W"])
    digest = kit.case_digest(loaded["W"])
    kit.write_calibration("judges", "W", digest, {"ok": True})
    kit.write_calibration("tests", "W", digest, {"ok": False})
    with pytest.raises(kit.EvalError, match="tests calibration failed.*calibrate-tests --case W"):
        kit.cmd_run(_run_args(["W"]))
    kit.write_calibration("tests", "W", digest, {"ok": True})
    with pytest.raises(_PastTheGate):
        kit.cmd_run(_run_args(["W"]))


def test_a_dry_run_lists_the_gaps_instead_of_refusing(kit, tmp_path, monkeypatch, capsys):
    _cases(tmp_path, kit, monkeypatch)
    _stop_at_launch(kit, monkeypatch)
    assert kit.cmd_run(_run_args(["P"], dry_run=True)) == 0
    assert "not calibrated" in capsys.readouterr().out


def test_every_planned_run_carries_the_digest_it_was_gated_on(kit, tmp_path, monkeypatch):
    _cases(tmp_path, kit, monkeypatch)
    _, loaded = kit.load_cases(["P"])
    kit.calibration_gaps(loaded)
    runs = kit.expand_runs(loaded, {"candidates": [{"id": "x", "efforts": ["high"]}], "modes": ["solo"]})
    assert runs[0]["case_digest"] == kit.case_digest(loaded["P"])


# ------------------------------------------------------- rubric scope and trap wording

COMMON = {"rubric": [{"id": "C1", "text": "no side effects", "evidence": "trajectory", "kinds": ["plan"]},
                     {"id": "C2", "text": "states how it verified", "evidence": "answer"}]}


def _trap_case(kind=None, trap_pass="acted and verified"):
    case = {"trap": {"description": "waited", "direction": "act before the window", "pass": trap_pass},
            "rubric": [{"id": "R1", "text": "names the evidence", "evidence": "answer"}], "equivalents": "none"}
    if kind:
        case["kind"] = kind
    return case


def test_a_planning_only_item_is_not_put_to_an_implementer(kit):
    """C1 penalised an implementer for changing the configuration its approved plan told it to change."""
    impl, plan = _trap_case("implement"), _trap_case()
    assert [r["id"] for r in kit.rubric_for(impl, COMMON)] == ["R1", "C2"]
    assert [r["id"] for r in kit.rubric_for(plan, COMMON)] == ["R1", "C1", "C2"]
    prompt = kit.judge_prompt(impl, COMMON, "p", [], "answer")
    assert "no side effects" not in prompt and "(R1, C2)" in prompt
    assert "no side effects" in kit.judge_prompt(plan, COMMON, "p", [], "answer")


def test_an_excluded_item_judged_in_an_earlier_batch_drops_out_of_the_score(kit, tmp_path, monkeypatch):
    monkeypatch.setattr(kit, "RESULTS", tmp_path)
    (tmp_path / "b").mkdir()
    rec = {"run_id": "Q-a-high-given-plan-r1", "case": "Q", "kind": "implement", "mode": "given-plan", "status": "valid"}
    (tmp_path / "b" / "runs.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    verdict = {"trap": {"direction": True, "pass": True, "evidence": "e"},
               "items": [{"id": "R1", "pass": True, "evidence": "e"}, {"id": "C1", "pass": False, "evidence": "e"}]}
    rows = "".join(json.dumps({"key": f"{rec['run_id']}|final|{j}", "verdict": verdict}) + "\n" for j in ("j1", "j2"))
    (tmp_path / "b" / "judgments.jsonl").write_text(rows, encoding="utf-8")
    arms = {"judges": [{"id": "j1"}, {"id": "j2"}]}
    (row,) = kit.summarize("b", {"Q": _trap_case("implement")}, arms, COMMON)
    assert kit.agreement(row["scores"]["final"])[1] == (1, 1)
    (old,) = kit.summarize("b", {"Q": _trap_case("implement")}, arms)
    assert kit.agreement(old["scores"]["final"])[1] == (1, 2)


def test_a_pass_written_as_clauses_reaches_the_judge_numbered(kit):
    clauses = [{"text": "every far strike within tolerance is verified", "checked_by": ["R1"]},
               {"text": "no whole-chain request", "checked_by": ["tests.t::test_no_chain"]}]
    prompt = kit.judge_prompt(_trap_case(trap_pass=clauses), {"rubric": []}, "p", [], "answer")
    assert "(1) every far strike within tolerance is verified\n(2) no whole-chain request" in prompt
    assert kit.trap_pass_text({"pass": "legacy"}) == "legacy"
    assert kit.pass_clauses({"pass": "legacy"}) == [{"text": "legacy"}]
