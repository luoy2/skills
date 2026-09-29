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


# ------------------------------------------------------------------- case lint

HIDDEN = '''\
import pytest
from pkg.selection import verify_strikes


class FakeApp:
    def __init__(self):
        self.errors = []


def test_a_query_failure_is_named(tmp_path):
    """A failure is not an unlisted strike."""
    app = FakeApp()
    with pytest.raises(RuntimeError, match="query failed"):
        verify_strikes(app, per_timeout=2.0)
    msg = str(app.errors)
    assert "strike 705 unavailable" in msg
    assert "SPXW" in msg and "354" in msg
    assert app.errors == []


def test_the_report_lists_what_it_skipped(caplog):
    report = verify_strikes(FakeApp())
    assert report.not_live[0]["position_uid"] == "u1"
    assert "Requested market data is not subscribed" in report.note
    assert any("timed out" in r.getMessage() for r in caplog.records)


def feed():
    return "Requested market data is not subscribed"
'''


def _impl_case(tmp_path, kit, monkeypatch, **fields):
    case_dir = tmp_path / "cases" / "W"
    (case_dir / "hidden").mkdir(parents=True)
    (case_dir / "hidden" / "test_sel.py.hidden").write_text(HIDDEN, encoding="utf-8")
    case = {"id": "W", "kind": "implement", "dir": case_dir, "snapshot": "0" * 40,
            "background": ["The fast path times out on META."], "owner_messages": ["fix it"],
            "hidden_tests": {"files": [{"file": "hidden/test_sel.py.hidden", "place": "tests/test_sel.py"}]},
            "trap": {"description": "d", "direction": "dir",
                     "pass": [{"text": "a failure is not a missing strike",
                               "checked_by": ["tests.test_sel::test_a_query_failure_is_named"]}]},
            "rubric": [{"id": "R1", "text": "t", "evidence": "e"}], "interface": [], **fields}
    monkeypatch.setattr(kit, "repo_tops", lambda sha: {"pkg"})
    monkeypatch.setattr(kit, "snapshot_names", lambda sha, names: {"verify_strikes", "per_timeout", "note"} & set(names))
    return case


def _values(result, level, check):
    return [f["value"] for f in result[level] if f["check"] == check]


def test_wording_a_hidden_test_asserts_must_be_disclosed(kit, tmp_path, monkeypatch):
    """W's tests asserted Chinese sentences nobody could guess: every implementer failed them."""
    case = _impl_case(tmp_path, kit, monkeypatch)
    result = kit.lint_case(case, {"implement_suffix": "go"})
    assert _values(result, "errors", "literal") == ["query failed", "strike 705 unavailable", "timed out"]
    where = [f["where"] for f in result["errors"] if f["value"] == "strike 705 unavailable"]
    assert where == ["hidden/test_sel.py.hidden:16"]
    case["interface"] = ["`verify_strikes` raises RuntimeError('query failed: strike 705 unavailable')"]
    case["background"].append("A silent request has timed out.")
    assert _values(kit.lint_case(case, {"implement_suffix": "go"}), "errors", "literal") == []


def test_identifiers_numbers_short_tokens_and_wording_the_test_feeds_in_are_not_guesses(kit, tmp_path, monkeypatch):
    case = _impl_case(tmp_path, kit, monkeypatch)
    literals = _values(kit.lint_case(case, {"implement_suffix": "go"}), "errors", "literal")
    assert "SPXW" not in literals and "354" not in literals and "u1" not in literals
    assert "Requested market data is not subscribed" not in literals


def test_a_name_the_snapshot_lacks_must_be_disclosed(kit, tmp_path, monkeypatch):
    """Q's tests read `report.not_live` entries that the plan never described."""
    case = _impl_case(tmp_path, kit, monkeypatch)
    result = kit.lint_case(case, {"implement_suffix": "go"})
    assert sorted(_values(result, "errors", "name")) == ["not_live", "position_uid"]
    case["interface"] = ["`report.not_live`: a list of dicts with `position_uid`"]
    assert _values(kit.lint_case(case, {"implement_suffix": "go"}), "errors", "name") == []


def test_a_given_plan_case_lists_the_tests_that_touch_undisclosed_names(kit, tmp_path, monkeypatch):
    case = _impl_case(tmp_path, kit, monkeypatch, modes=["given-plan"])
    docs = kit.lint_case(case, {"implement_suffix": "go"})["docstrings"]
    assert [d["test"] for d in docs] == ["tests.test_sel::test_a_query_failure_is_named",
                                         "tests.test_sel::test_the_report_lists_what_it_skipped"]
    assert docs[0]["docstring"] == "A failure is not an unlisted strike."
    assert "not_live" in docs[1]["names"]
    assert kit.lint_case(_impl_case(tmp_path / "direct", kit, monkeypatch), {"implement_suffix": "go"})["docstrings"] == []


def test_a_pass_clause_must_cite_checks_that_exist(kit, tmp_path, monkeypatch):
    case = _impl_case(tmp_path, kit, monkeypatch)
    case["trap"]["pass"] += [{"text": "filters by class", "checked_by": ["R1", "tests.test_sel::test_gone", "C9"]},
                             {"text": "unmapped clause"}]
    result = kit.lint_case(case, {"implement_suffix": "go"})
    assert _values(result, "errors", "checked_by") == ["tests.test_sel::test_gone", "C9"]
    assert [f["where"] for f in result["warnings"] if f["check"] == "unmapped"] == ["trap.pass (3)"]
    case["trap"]["pass"] = "one string"
    assert [f["check"] for f in kit.lint_case(case, {"implement_suffix": "go"})["warnings"]] == ["pass_string"]


def test_trap_wording_and_wrong_markers_are_flagged_for_planning_cases_too(kit, tmp_path, monkeypatch):
    case_dir = tmp_path / "cases" / "A"
    case_dir.mkdir(parents=True)
    (case_dir / "plan.md").write_text("intro\nkeep infra/broker/ for now\n", encoding="utf-8")
    case = {"id": "A", "dir": case_dir, "background": ["the owner asked", "machine directories stay as they are"],
            "owner_messages": ["m"], "attachments": [{"file": "plan.md", "inline": True}],
            "trap": {"description": "d", "direction": "as the recommended default, not as an option with preconditions",
                     "pass": [{"text": "p", "checked_by": ["R1"]}], "wrong_markers": ["infra/(broker|pisces)/", "stay as"]},
            "rubric": [{"id": "R1", "text": "t", "evidence": "e"}]}
    monkeypatch.setattr(kit, "CFG", {"lint": {"direction_forbidden": ["not as an option with preconditions"]}})
    result = kit.lint_case(case, {"suffix": "s"})
    assert result["errors"] == []
    assert [(f["check"], f["where"]) for f in result["warnings"]] == [
        ("direction", "trap.direction"), ("wrong_marker", "plan.md:2"), ("wrong_marker", "background:2")]
    case["trap"]["wrong_markers"] = ["("]
    assert _values(kit.lint_case(case, {"suffix": "s"}), "errors", "bad_regex") == ["("]


# ------------------------------------------------ scoring denominators and alternatives

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_design[x]"/>
<testcase classname="tests.test_a" name="test_design[y]"><failure message="x"/></testcase>
<testcase classname="tests.test_a" name="test_bad"><failure message="x"/></testcase>
<testcase classname="" name="tests.test_b"><error message="collection failure"/></testcase>
</testsuite></testsuites>"""


def test_reference_only_tests_run_but_leave_the_score_and_its_denominator(kit, tmp_path):
    """A test encoding the real fix's own design would otherwise cap every candidate below the reference."""
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT, encoding="utf-8")
    got = kit.parse_junit(path, 10, ["tests.test_a::test_design[x]", "tests.test_a::test_design[y]"])
    assert (got["passed"], got["failed"], got["errors"], got["scored_expected"]) == (1, 1, 1, 8)
    assert got["reference_only_passed"] == 1 and got["pass_rate"] == 0.125
    assert got["cases"] == {"tests.test_a::test_ok": "passed", "tests.test_a::test_design[x]": "passed",
                            "tests.test_a::test_design[y]": "failed", "tests.test_a::test_bad": "failed",
                            "tests.test_b": "error"}
    plain = kit.parse_junit(path, 10)
    assert (plain["passed"], plain["scored_expected"], plain["pass_rate"]) == (2, 10, 0.2)


def test_an_alternative_may_fail_only_reference_only_or_accepted_tests(kit):
    """W's tests failed a reasonable plan's implementation for its design, not for a defect."""
    reference = {"t::a": "passed", "t::b": "passed", "t::c": "passed", "t::d": "passed", "t::e": "failed"}
    alt = {"t::a": "passed", "t::b": "failed", "t::c": "error", "t::e": "failed"}
    assert kit.alt_failures(reference, alt) == ["t::b", "t::c", "t::d"]
    assert kit.alt_failures(reference, alt, ["t::b"], {"t::c": "patch drops the error code"}) == ["t::d"]
