"""agent-eval runner: the deterministic parts a conclusion rests on.

An implementation run is scored by hidden tests and by judges reading the diff.
Each piece below fails silently when wrong: a collection error read as a small
denominator inflates a pass rate, a missed commit shrinks the diff the judges
see, a plan written per implementer breaks the shared-plan comparison, and an
inherited credential or native login turns the sandbox into a hole.
"""

from __future__ import annotations

import importlib.util
import subprocess
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


def _git(wt, *args):
    return subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_the_diff_includes_commits_edits_and_new_files_against_the_snapshot(kit, tmp_path):
    """A candidate that commits part of its work must not hide that part from the judges."""
    wt, out = tmp_path / "wt", tmp_path / "out"
    wt.mkdir()
    out.mkdir()
    (wt / "a.py").write_text("x = 1\n")
    _git(wt, "init", "-q")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "snapshot")
    base = _git(wt, "rev-parse", "HEAD")
    (wt / "a.py").write_text("x = 2\n")
    _git(wt, "commit", "-qam", "candidate commit")
    (wt / "b.py").write_text("y = 1\nz = 2\n")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_b.py").write_text("def test_b():\n    pass\n")
    stat = kit.capture_diff(wt, base, out)
    assert stat == {"files": 3, "added": 5, "deleted": 1, "test_files": 1}
    patch = (out / "diff.patch").read_text()
    assert "x = 2" in patch and "z = 2" in patch


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


def test_an_overlay_replaces_instruction_files_inside_the_snapshot_commit(kit, monkeypatch, tmp_path):
    """An SOP rerun changes only the overlaid files, and an implementer's diff never shows them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("old rules\n", encoding="utf-8")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    overlay = tmp_path / "overlay"
    (overlay / "docs").mkdir(parents=True)
    (overlay / "AGENTS.md").write_text("new rules\n", encoding="utf-8")
    (overlay / "docs" / "process.md").write_text("process\n", encoding="utf-8")
    monkeypatch.setattr(kit, "REPO", repo)
    monkeypatch.setattr(kit, "OVERLAY_DIR", overlay)
    wt = kit.prepare_snapshot({"snapshot": _git(repo, "rev-parse", "HEAD")}, tmp_path / "run")
    assert (wt / "AGENTS.md").read_text(encoding="utf-8") == "new rules\n"
    assert (wt / "docs" / "process.md").read_text(encoding="utf-8") == "process\n"
    assert (wt / "code.py").read_text(encoding="utf-8") == "x = 1\n"
    assert _git(wt, "status", "--porcelain") == ""
    assert set(kit.overlay_files()) == {"AGENTS.md", "docs/process.md"}


def test_planning_repeats_name_each_run_and_leave_single_runs_unchanged(kit):
    cases = {"I": {"id": "I"}}
    arms = {"candidates": [{"id": "astra", "efforts": ["xhigh"]}], "modes": ["solo"]}
    assert [r["run_id"] for r in kit.expand_runs(cases, arms)] == ["I-astra-xhigh-solo"]
    assert [r["run_id"] for r in kit.expand_runs(cases, arms, repeats=2)] == \
        ["I-astra-xhigh-solo-r1", "I-astra-xhigh-solo-r2"]


def test_a_shared_plan_is_not_leak_checked_but_its_marker_hits_are_recorded(kit, monkeypatch, tmp_path):
    """An isolated planner may name the fix's test file by the repository's convention; that is no leak."""
    monkeypatch.setattr(kit, "SCRATCH_ROOT", tmp_path / "scratch")
    monkeypatch.setattr(kit, "RESULTS", tmp_path / "results")

    def fake_snapshot(case, root):
        wt = root / "wt"
        for sub in ("wt", "home", "tmp"):
            (root / sub).mkdir(parents=True)
        (wt / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(wt, "init", "-q")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "snapshot")
        return wt

    seen = {}

    def fake_check(root, markers, prompt, *args, **kwargs):
        seen["prompt"] = prompt
        return ["stopped by the test"]

    monkeypatch.setattr(kit, "prepare_snapshot", fake_snapshot)
    monkeypatch.setattr(kit, "isolation_check", fake_check)
    case = {"id": "W", "kind": "implement", "snapshot": "abc", "background": ["b"], "owner_messages": ["m"],
            "leak_markers": ["test_fix_name"]}
    common = {"implement_suffix": "do it", "split_plan_intro": "plan below", "leak_markers": []}
    cand = {"id": "sol", "model": "m", "runtime": "codex", "client": "gateway"}
    run = {"run_id": "W-sol-high-split-p-r1", "case": "W", "candidate": cand, "effort": "high", "mode": "split-p", "rep": 1}
    plans = {kit.plan_run_id("W", "p", 1): {"status": "valid", "final": "add tests/test_fix_name.py"}}
    rec = kit.execute_impl_run(run, "b1", common, {"W": case}, {}, {}, "key", 60, plans)
    assert "test_fix_name" not in seen["prompt"]
    assert rec["plan_marker_hits"] == ["test_fix_name"]
    assert rec["reasons"] == ["isolation: stopped by the test"]
