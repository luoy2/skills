"""i-have-ocd store run the way sessions run it: one process per command, several at once.

Parks from many processes at the same moment all land, each under its own id, even when the
first of them creates the store; of several sessions taking one item from the same version,
exactly one gets the lease; every worktree of a repository finds the same store; and the real
entry point prints `?` for a store it cannot read. Every case here starts a process;
tests/test_ocd_store.py holds the rest of the contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "skills" / "engineering" / "i-have-ocd" / "scripts" / "ocd.py"


def _env(home, **extra):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "I_HAVE_OCD_HOME": str(home / "state")}
    return {**env, **extra}


def _start(argv, env, cwd=None):
    return subprocess.Popen([sys.executable, str(SCRIPT), *map(str, argv)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)


def _run(argv, env, cwd=None):
    proc = _start(argv, env, cwd)
    out, err = proc.communicate(timeout=60)
    return proc.returncode, out, err


def test_concurrent_parks_from_several_processes_lose_nothing(tmp_path):
    env = _env(tmp_path, I_HAVE_OCD_PROJECT="t")  # no store yet: the first parks also race to create it
    procs = [_start(["park", f"finding {n}", "--source", f"session {n}", "--request-id", f"r{n}", "--by", f"s{n}"], env)
             for n in range(16)]
    errors = [p.communicate(timeout=60)[1] for p in procs]
    assert all(p.returncode == 0 for p in procs), errors
    code, out, _ = _run(["list"], env)
    items = json.loads(out)["items"]
    assert sorted(i["summary"] for i in items) == sorted(f"finding {n}" for n in range(16))
    assert len({i["id"] for i in items}) == 16
    view = (tmp_path / "state" / "t.md").read_text(encoding="utf-8")
    assert all(f"· finding {n} ·" in view for n in range(16))
    assert _run(["count"], env)[1].strip() == "16"
    assert sorted(path.name for path in (tmp_path / "state").iterdir()) == ["t.db", "t.md"]


def test_one_of_several_sessions_taking_the_same_version_gets_the_lease(tmp_path):
    env = _env(tmp_path, I_HAVE_OCD_PROJECT="t")
    assert _run(["park", "one", "--request-id", "p"], env)[0] == 0
    procs = [_start(["take", "P1", "--expected-version", 1, "--request-id", f"t{n}", "--by", f"s{n}"], env)
             for n in range(6)]
    for p in procs:
        p.communicate(timeout=60)
    codes = [p.returncode for p in procs]
    assert sorted(codes) == [0, 3, 3, 3, 3, 3]
    item = json.loads(_run(["show", "P1"], env)[1])["item"]
    assert item["version"] == 2 and item["claim"]["holder"] == f"s{codes.index(0)}"


def test_every_worktree_of_a_repository_is_one_project(tmp_path):
    repo, worktree = tmp_path / "myrepo", tmp_path / "elsewhere" / "feature-x"
    env = _env(tmp_path, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com")
    for argv in (["init", "-q", str(repo)], ["-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
                 ["-C", str(repo), "worktree", "add", "-q", "-b", "feature-x", str(worktree)]):
        subprocess.run(["git", *argv], check=True, env=env, capture_output=True)
    assert _run(["park", "seen in the worktree", "--request-id", "w"], env, cwd=worktree)[0] == 0
    assert (tmp_path / "state" / "myrepo.db").exists()
    assert _run(["count"], env, cwd=repo)[1].strip() == "1"
    outside = tmp_path / "plain-dir"
    outside.mkdir()
    assert _run(["count"], env, cwd=outside)[:2] == (1, "?\n")


def test_the_entry_point_prints_a_question_mark_for_a_store_it_cannot_read(tmp_path):
    env = _env(tmp_path, I_HAVE_OCD_PROJECT="t")
    code, out, err = _run(["count"], env)
    assert (code, out) == (1, "?\n") and "no store" in err
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "t.db").write_bytes(b"\x00garbage" * 512)
    code, out, err = _run(["count"], env)
    assert (code, out) == (1, "?\n") and err
