"""agent-eval isolation check cases that scan a run root for planted answers and markers,
which runs grep, so they live under tests/integration/; tests/test_evalkit_planning.py keeps
the rest of the check.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"


@pytest.fixture(scope="module")
def kit():
    spec = importlib.util.spec_from_file_location("evalkit_planning_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root(tmp_path):
    for sub in ("wt", "home", "tmp"):
        (tmp_path / sub).mkdir()
    (tmp_path / "wt" / "AGENTS.md").write_text("# entry\n")
    return tmp_path


def test_a_planted_answer_in_the_home_fails_the_check(kit, tmp_path):
    root = _root(tmp_path)
    (root / "home" / "notes.md").write_text("the fix is to rotate the ledger\n")
    failures = kit.isolation_check(root, ["rotate the ledger"], "prompt", {}, "claude", "native",
                                   argv=kit.claude_argv("m", "high"))
    assert any("leak markers" in f and "notes.md" in f for f in failures), failures


def test_a_clean_root_passes(kit, tmp_path):
    root = _root(tmp_path)
    assert kit.isolation_check(root, ["rotate the ledger"], "prompt", {}, "claude", "native",
                               argv=kit.claude_argv("m", "high")) == []


def test_a_marker_inside_the_prompt_fails_the_check(kit, tmp_path):
    root = _root(tmp_path)
    failures = kit.isolation_check(root, ["#1234"], "see #1234", {}, "claude", "native",
                                   argv=kit.claude_argv("m", "high"))
    assert any("<prompt>" in f for f in failures)
