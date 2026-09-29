"""agent-eval case lint against a real snapshot: the name check reads the snapshot through git.
tests/test_evalkit_case_checks.py states what each check guards.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"


@pytest.fixture
def kit():
    spec = importlib.util.spec_from_file_location("evalkit_case_checks_integration", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(wt, *args):
    return subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


HIDDEN = '''\
import json
from pkg.liveness import quote_liveness, REASONS


def test_the_verdict_names_its_reason(tmp_path):
    got = quote_liveness({}, None, now=1.0, hard_cap_s=5, farm_state="down")
    assert got.reason in REASONS and got.md_farm_ok is None
    assert json.dumps(got.as_row()).startswith("{")
'''


@pytest.fixture
def snapshot_case(kit, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "liveness.py").write_text(
        "REASONS = ()\n\ndef quote_liveness(quote, underlying, *, now, hard_cap_s):\n    return quote\n",
        encoding="utf-8")
    (repo / "data.bin").write_bytes(b"\0md_farm_ok\0")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "snapshot")
    cases = tmp_path / "cases"
    (cases / "Q" / "hidden").mkdir(parents=True)
    (cases / "Q" / "hidden" / "test_live.py.hidden").write_text(HIDDEN, encoding="utf-8")
    (cases / "common.json").write_text(json.dumps({"rubric": [], "implement_suffix": "go"}), encoding="utf-8")
    case = {"id": "Q", "kind": "implement", "title": "t", "snapshot": _git(repo, "rev-parse", "HEAD"),
            "background": ["b"], "owner_messages": ["m"], "interface": ["`quote_liveness(quote, underlying, *, now, hard_cap_s)`"],
            "hidden_tests": {"files": [{"file": "hidden/test_live.py.hidden", "place": "tests/test_live.py"}],
                             "paths": ["tests"], "expected": 1},
            "trap": {"description": "d", "direction": "dir",
                     "pass": [{"text": "p", "checked_by": ["tests.test_live::test_the_verdict_names_its_reason"]}]},
            "rubric": []}
    (cases / "Q" / "case.json").write_text(json.dumps(case), encoding="utf-8")
    monkeypatch.setattr(kit, "REPO", repo)
    monkeypatch.setattr(kit, "CASES", cases)
    monkeypatch.setattr(kit, "SCRATCH_ROOT", tmp_path / "scratch")
    return cases / "Q" / "case.json"


def test_names_the_snapshot_lacks_and_nobody_disclosed_are_errors(kit, snapshot_case):
    """`md_farm_ok` sits only in a binary file and `farm_state`/`reason`/`as_row` nowhere: none is guessable."""
    common, cases = kit.load_cases(["Q"])
    result = kit.lint_case(cases["Q"], common)
    assert sorted(f["value"] for f in result["errors"] if f["check"] == "name") == \
        ["as_row", "farm_state", "md_farm_ok", "reason"]
    assert result["warnings"] == []


def test_lint_case_exits_one_on_an_error_and_zero_once_disclosed(kit, snapshot_case, capsys):
    common, _ = kit.load_cases(["Q"])
    args = type("Args", (), {"case": "Q"})()
    assert kit.cmd_lint_case(args) == 1
    assert "farm_state" in capsys.readouterr().out
    case = json.loads(snapshot_case.read_text(encoding="utf-8"))
    case["interface"].append("`quote_liveness(..., farm_state)` returns a verdict with `reason`, `md_farm_ok` "
                             "and `as_row()`")
    snapshot_case.write_text(json.dumps(case), encoding="utf-8")
    assert kit.cmd_lint_case(args) == 0


def _patch_adding(name):
    return (f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+x\n")


@pytest.fixture
def calibration_case(kit, tmp_path, monkeypatch):
    """A case whose tests read which patch was applied from marker files in the tree."""
    cases = tmp_path / "cases"
    (cases / "W").mkdir(parents=True)
    (cases / "common.json").write_text(json.dumps({"rubric": [], "implement_suffix": "go"}), encoding="utf-8")
    (cases / "W" / "reference.patch").write_text(_patch_adding("REF"), encoding="utf-8")
    (cases / "W" / "alt.patch").write_text(_patch_adding("ALT"), encoding="utf-8")
    case = {"id": "W", "kind": "implement", "title": "t", "snapshot": "0" * 40, "background": ["b"],
            "owner_messages": ["m"], "reference_patch": "reference.patch",
            "hidden_tests": {"files": [], "paths": ["tests"], "expected": 3, "reference_only": ["t::design"]},
            "trap": {"description": "d", "direction": "dir", "pass": [{"text": "p", "checked_by": []}]}, "rubric": []}

    def write(**fields):
        (cases / "W" / "case.json").write_text(json.dumps({**case, **fields}), encoding="utf-8")

    def fake_snapshot(case, root):
        wt = root / "wt"
        if root.exists():
            import shutil
            shutil.rmtree(root)
        for sub in ("wt", "home", "tmp"):
            (root / sub).mkdir(parents=True)
        (wt / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(wt, "init", "-q")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "snapshot")
        return wt

    def fake_tests(case, root, out, timeout):
        wt = Path(root) / "wt"
        ref, alt = (wt / "REF").exists(), (wt / "ALT").exists()
        outcomes = {"t::behaviour": "passed" if ref or alt else "failed", "t::design": "passed" if ref else "failed",
                    "t::wording": "passed" if ref else "failed"}
        passed = sum(v == "passed" for k, v in outcomes.items() if k != "t::design")
        return {"passed": passed, "scored_expected": 2, "expected": 3, "reference_only_passed": int(ref),
                "cases": outcomes, "junit": True}

    monkeypatch.setattr(kit, "CASES", cases)
    monkeypatch.setattr(kit, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(kit, "SCRATCH_ROOT", tmp_path / "scratch")
    monkeypatch.setattr(kit, "CFG", {"implement": {"test_timeout": 5}})
    monkeypatch.setattr(kit, "prepare_snapshot", fake_snapshot)
    monkeypatch.setattr(kit, "prepare_sandbox", lambda root: None)
    monkeypatch.setattr(kit, "run_hidden_tests", fake_tests)
    monkeypatch.setattr(kit, "lint_case", lambda case, common: {"case": "W", "errors": [], "warnings": [], "docstrings": []})
    return write


def _tests_record(kit):
    _, cases = kit.load_cases(["W"])
    return kit.read_calibration("tests", "W", kit.case_digest(cases["W"]))


def test_calibrate_tests_fails_a_case_without_an_alternative_implementation(kit, calibration_case):
    calibration_case()
    assert kit.cmd_calibrate_tests(type("Args", (), {"case": "W"})()) == 1
    rec = _tests_record(kit)
    assert rec["ok"] is False and any("no alt_patches" in p for p in rec["problems"])
    assert rec["reference"]["passed"] == 2 and rec["reference"]["reference_only_passed"] == 1


def test_calibrate_tests_names_what_an_alternative_fails_until_the_case_excuses_it(kit, calibration_case):
    calibration_case(alt_patches=[{"file": "alt.patch", "note": "the astra plan", "accepted_failures": {}}])
    assert kit.cmd_calibrate_tests(type("Args", (), {"case": "W"})()) == 1
    (alt,) = _tests_record(kit)["alts"]
    assert alt["unexcused_failures"] == ["t::wording"]
    calibration_case(alt_patches=[{"file": "alt.patch", "note": "the astra plan",
                                   "accepted_failures": {"t::wording": "drops the strike from the message"}}])
    assert kit.cmd_calibrate_tests(type("Args", (), {"case": "W"})()) == 0
    assert _tests_record(kit)["ok"] is True
