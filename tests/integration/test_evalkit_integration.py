"""agent-eval runner cases that start another process: the diff the judges read, the
instruction overlay inside the snapshot commit and the shared plan's marker scan (git), and the
batch lock (a second process holding it). tests/test_evalkit.py states what each piece guards.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
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


def test_a_second_writer_of_a_batch_is_refused_until_the_first_exits(kit, monkeypatch, tmp_path, capsys):
    """Two `run`s of one batch share its run directories: the second must not start."""
    monkeypatch.setattr(kit, "RESULTS", tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import importlib.util, pathlib, sys
        spec = importlib.util.spec_from_file_location("k", {str(KIT_PATH)!r})
        k = importlib.util.module_from_spec(spec); spec.loader.exec_module(k)
        k.RESULTS = pathlib.Path({str(tmp_path)!r})
        fh = k.hold_batch("b"); print("held", flush=True); sys.stdin.readline()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(kit.EvalError, match=r"batch b is being written by pid \d+"):
            kit.hold_batch("b")
        kit.hold_batch("other").close()  # another batch is free
        config = json.loads((KIT_PATH.parents[1] / "assets" / "config.example.json").read_text(encoding="utf-8"))
        config.update({"repo": str(tmp_path), "cases_dir": str(tmp_path / "cases"), "results_dir": str(tmp_path)})
        (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("evalkit_cli_under_test", KIT_PATH)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        assert cli.main(["--config", str(tmp_path / "config.json"), "judge", "--batch", "b"]) == 2
        assert "is being written by pid" in capsys.readouterr().err
    finally:
        holder.communicate("\n", timeout=30)
    kit.hold_batch("b").close()  # the lock left with the process



def test_rescore_rebuilds_the_tree_the_candidate_left_from_its_saved_diff(kit, monkeypatch, tmp_path):
    """Scoring again must see the candidate's tree: its edits, new files, deletions and placed attachments."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "gone.py").write_text("old\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "plan.md").write_text("plan\n")
    case = {"snapshot": _git(repo, "rev-parse", "HEAD"), "dir": case_dir,
            "attachments": [{"file": "plan.md", "place": "docs/plan.md"}]}
    monkeypatch.setattr(kit, "REPO", repo)
    monkeypatch.setattr(kit, "OVERLAY_DIR", None)
    wt = kit.prepare_snapshot(case, tmp_path / "run")
    base = _git(wt, "rev-parse", "HEAD")
    (wt / "a.py").write_text("x = 2\n")
    (wt / "new.py").write_text("y = 1\n")
    (wt / "gone.py").unlink()
    (wt / "docs" / "plan.md").write_text("plan, amended\n")
    out = tmp_path / "out"
    out.mkdir()
    kit.capture_diff(wt, base, out)
    left = {p.relative_to(wt).as_posix(): p.read_text() for p in wt.rglob("*") if p.is_file() and ".git" not in p.parts}
    again = kit.rebuild_tree(case, tmp_path / "again", out / "diff.patch")
    rebuilt = {p.relative_to(again).as_posix(): p.read_text() for p in again.rglob("*")
               if p.is_file() and ".git" not in p.parts}
    assert rebuilt == left


def test_rescore_refuses_a_diff_that_does_not_apply(kit, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    monkeypatch.setattr(kit, "REPO", repo)
    monkeypatch.setattr(kit, "OVERLAY_DIR", None)
    patch = tmp_path / "diff.patch"
    patch.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 9\n+x = 2\n")
    with pytest.raises(kit.EvalError, match="does not apply"):
        kit.rebuild_tree({"snapshot": _git(repo, "rev-parse", "HEAD"), "dir": tmp_path}, tmp_path / "run", patch)
