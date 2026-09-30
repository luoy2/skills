"""agent-eval runner: the deterministic parts a conclusion rests on.

An implementation run is scored by hidden tests and by judges reading the diff.
Each piece below fails silently when wrong: a collection error read as a small
denominator inflates a pass rate, a missed commit shrinks the diff the judges
see, a plan written per implementer breaks the shared-plan comparison, and an
inherited credential or native login turns the sandbox into a hole.
The cases that build a git repository are in tests/integration/test_evalkit_integration.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"


@pytest.fixture(scope="module")
def kit():
    spec = importlib.util.spec_from_file_location("evalkit_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">
<testcase classname="tests.test_a" name="test_ok_1"/>
<testcase classname="tests.test_a" name="test_ok_2"/>
<testcase classname="tests.test_a" name="test_bad"><failure message="x"/></testcase>
<testcase classname="" name="tests.test_b"><error message="collection failure"/></testcase>
<testcase classname="tests.test_a" name="test_skip"><skipped message="s"/></testcase>
</testsuite></testsuites>"""


def test_a_file_that_fails_to_collect_scores_against_the_expected_total(kit, tmp_path):
    """Four tests ran, but the case expects 40: the missing file's tests are zero passes, not absent."""
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT, encoding="utf-8")
    got = kit.parse_junit(path, 40)
    assert (got["passed"], got["failed"], got["errors"], got["skipped"]) == (2, 1, 1, 1)
    assert got["pass_rate"] == 0.05


def test_no_junit_report_is_a_zero_not_a_crash(kit, tmp_path):
    got = kit.parse_junit(tmp_path / "missing.xml", 42)
    assert got["junit"] is False and got["passed"] == 0 and got["pass_rate"] == 0.0


def test_a_long_diff_is_cut_in_its_tests_first_and_names_what_it_left_out(kit, tmp_path):
    out = tmp_path
    src = "diff --git a/trading/x.py b/trading/x.py\n+" + "s" * 50 + "\n"
    tst = "diff --git a/tests/test_x.py b/tests/test_x.py\n+" + "t" * 50 + "\n"
    (out / "diff.patch").write_text(tst + src, encoding="utf-8")
    rec = {"final": "done", "diff": {"files": 2, "added": 2, "deleted": 0}}
    answer = kit.impl_answer(rec, out, limit=len(src) + 10)
    assert "s" * 50 in answer and "t" * 50 not in answer
    assert "tests/test_x.py" in answer.split("diff omitted for length")[1]


IMPL = {"modes": ["direct", "split-astra-xhigh"], "repeats": 2,
        "candidates": [{"id": "sol", "model": "gpt-6-sol", "runtime": "codex", "client": "gateway",
                        "efforts": ["medium", "high"]},
                       {"id": "luna", "model": "gpt-6-luna", "runtime": "codex", "client": "gateway",
                        "efforts": ["medium"]}]}


def test_every_implementer_in_a_split_mode_shares_one_plan_per_case_and_repeat(kit):
    """Three arms × two repeats need two plans, not six: the comparison is the implementer, not the plan."""
    runs = kit.expand_impl_runs({"W": {"id": "W"}}, IMPL)
    assert len(runs) == 3 * 2 * 2
    plans = [kit.plan_run_id(*needed) for needed in kit.plans_needed(runs)]
    assert plans == ["W-plan-astra-xhigh-r1", "W-plan-astra-xhigh-r2"]
    assert kit.planner_of("direct") is None and kit.planner_of("given-plan") is None


def test_a_case_can_narrow_the_delivery_modes(kit):
    runs = kit.expand_impl_runs({"Q": {"id": "Q", "modes": ["given-plan"]}}, IMPL, repeats=1)
    assert {r["mode"] for r in runs} == {"given-plan"} and len(runs) == 3


def test_the_implementer_environment_inherits_nothing(kit, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "secret")
    monkeypatch.setattr(kit, "TEST_PYTHON", "/venv/bin/python")
    cand = {"id": "opus55", "runtime": "claude", "client": "gateway"}
    env = kit.impl_env(tmp_path, "k", cand)
    assert kit.forbidden_env(env, "claude", "gateway") == []
    assert env["PATH"].split(":")[0] == "/venv/bin"
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path))
    with pytest.raises(kit.EvalError):
        kit.impl_env(tmp_path, "k", {**cand, "client": "native"})


def test_the_sandbox_profile_opens_only_the_configured_extra_reads(kit, monkeypatch, tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setattr(kit, "ALLOW_READ", (str(venv),))
    profile = kit.sandbox_profile(tmp_path / "run")
    assert f'(allow file-read-data (subpath "{venv}"))' in profile
    assert profile.count("(allow file-read-data") == 2  # the run directory and the venv, nothing else


def _row(run_id, case, **kw):
    base = {"run_id": run_id, "case": case, "candidate": "sol", "model": "gpt-6-sol", "effort": "high",
            "status": "valid", "reasons": [], "elapsed_s": 10, "scores": {"final": {}}}
    return {**base, **kw}


def test_the_archived_report_pays_a_shared_plan_once(kit, monkeypatch, tmp_path):
    """Two implementers on one $6 plan cost $6 + their own work, not $12 + their work."""
    monkeypatch.setattr(kit, "RESULTS", tmp_path)
    case = {"id": "W", "title": "t", "kind": "implement", "hidden_tests": {"expected": 42, "baseline": 3}}
    rows = [_row(f"W-{c}-high-split-p-r1", "W", mode="split-p", rep=1, cost_usd=1.0, chain_cost_usd=7.0,
                 plan_run="W-plan-p-r1", plan_cost_usd=6.0, tests={"passed": 30}) for c in ("a", "b")]
    rows.append(_row("W-c-high-direct-r1", "W", mode="direct", rep=1, cost_usd=2.0, chain_cost_usd=2.0,
                     tests={"passed": 20}))
    md = kit.render_markdown("b", rows, {"W": case}, {"judges": []})
    assert "run cost $10.00" in md


def test_a_batch_judged_before_the_two_level_trap_shows_no_direction(kit):
    per = {"j1": {"trap": False, "direction": None, "items": {}},
           "j2": {"trap": False, "direction": None, "items": {}}}
    assert kit._verdict(per) == ("—", False)
    per["j1"]["direction"], per["j2"]["direction"] = True, False
    assert kit._verdict(per) == (None, False)


def test_the_archived_report_lists_a_plan_run_whose_judges_split_on_direction(kit):
    """A split on either level is a dispute, in plan cases as in implementation cases."""
    case = {"id": "P", "title": "t", "kind": "plan"}
    split = {"j1": {"trap": True, "direction": True, "items": {}},
             "j2": {"trap": True, "direction": False, "items": {}}}
    rows = [_row("P-sol-high-solo", "P", mode="solo", scores={"final": split})]
    md = kit.render_markdown("b", rows, {"P": case}, {"judges": []})
    assert "P-sol-high-solo draft: trap" in md


def test_planning_repeats_name_each_run_and_leave_single_runs_unchanged(kit):
    cases = {"I": {"id": "I"}}
    arms = {"candidates": [{"id": "astra", "efforts": ["xhigh"]}], "modes": ["solo"]}
    assert [r["run_id"] for r in kit.expand_runs(cases, arms)] == ["I-astra-xhigh-solo"]
    assert [r["run_id"] for r in kit.expand_runs(cases, arms, repeats=2)] == \
        ["I-astra-xhigh-solo-r1", "I-astra-xhigh-solo-r2"]


def test_the_archived_report_names_the_overlaid_instruction_files(kit, monkeypatch, tmp_path):
    """A rerun under a new SOP must say so in the archive, or it reads as the original snapshot."""
    monkeypatch.setattr(kit, "RESULTS", tmp_path)
    case = {"id": "W", "title": "t", "kind": "implement", "hidden_tests": {"expected": 42, "baseline": 3}}
    rows = [_row("W-a-high-direct-r1", "W", mode="direct", rep=1, cost_usd=2.0, chain_cost_usd=2.0,
                 tests={"passed": 20}, overlay={"AGENTS.md": "abc123"})]
    assert "overlay_dir" in kit.render_markdown("b", rows, {"W": case}, {"judges": []})
    assert "`AGENTS.md`" in kit.render_markdown("b", rows, {"W": case}, {"judges": []})
    plain = [{**rows[0], "overlay": {}}]
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "runs.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    assert kit.summarize("b", {"W": case}, {"judges": []})[0]["overlay"] == {"AGENTS.md": "abc123"}
    assert "overlay_dir" not in kit.render_markdown("b", plain, {"W": case}, {"judges": []})


def _review(run_id, case="I"):
    return {"run_id": run_id, "case": case, "candidate": "sol", "effort": "high", "mode": "review", "status": "valid",
            "draft": "draft", "stages": [{"stage": "review", "final": "review"}]}


def test_repeated_review_sources_adopt_into_separate_runs(kit):
    """Two repeats of one review arm used to share `…-adopt-r1`: one run directory, one ledger entry."""
    runs = kit.adopt_runs([_review("I-sol-high-review-r1"), _review("I-sol-high-review-r2"),
                           _review("H-sol-high-review", case="H")], None, None, 1)
    assert [r["run_id"] for r in runs] == ["H-sol-high-adopt-r1", "I-sol-high-adopt-s1-r1", "I-sol-high-adopt-s2-r1"]
    assert [r["source_run"] for r in runs] == ["H-sol-high-review", "I-sol-high-review-r1", "I-sol-high-review-r2"]


def test_recompute_refuses_an_implementation_batch_and_leaves_it_untouched(kit, monkeypatch, tmp_path):
    """Its cost and validity rules are the planning runner's; an implementation run would come back wrong."""
    monkeypatch.setattr(kit, "RESULTS", tmp_path)
    ledger = tmp_path / "i1" / "runs.jsonl"
    ledger.parent.mkdir()
    ledger.write_text(json.dumps({"run_id": "W-a-high-direct-r1", "kind": "implement", "status": "invalid",
                                  "stages": [{"stage": "implement"}], "chain_cost_usd": 7.0}) + "\n", encoding="utf-8")
    before = ledger.read_bytes()
    with pytest.raises(kit.EvalError, match="planning runs only"):
        kit.cmd_recompute(type("Args", (), {"batch": "i1"})())
    assert ledger.read_bytes() == before
