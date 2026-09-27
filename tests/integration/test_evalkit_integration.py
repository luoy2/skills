"""agent-eval runner cases that build a git repository: the diff the judges read, the
instruction overlay inside the snapshot commit, and the shared plan's marker scan. They start
git, so they live under tests/integration/; tests/test_evalkit.py states what each piece guards.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"


@pytest.fixture(scope="module")
def kit():
    spec = importlib.util.spec_from_file_location("evalkit_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
