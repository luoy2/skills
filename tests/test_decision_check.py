"""decision-check: the questionnaire hook a project registers in .claude/settings.json.

Each questionnaire is denied once with the project's checklist as the reason, and
the model's retry from the same session goes through; other tools are untouched,
and a failure lets the question through rather than keep the owner from being
asked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "engineering" / "questionnaire-review"
HOOK = SKILL / "scripts" / "decision-check.mjs"
EXAMPLE = SKILL / "assets" / "owner-decisions.example.md"
EXAMPLE_ARG = str(EXAMPLE.relative_to(REPO_ROOT))


def _run(payload, state, project=REPO_ROOT, args=(EXAMPLE_ARG,), env_extra=None):
    node = shutil.which("node")
    assert node, "node is required: Claude Code runs this hook with node"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "CLAUDE_PROJECT_DIR": str(project),
           "DECISION_CHECK_STATE_DIR": str(state), **(env_extra or {})}
    proc = subprocess.run([node, str(HOOK), *args], input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _ask(session="s1"):
    return {"session_id": session, "tool_name": "AskUserQuestion", "tool_use_id": "toolu_1",
            "tool_input": {"questions": []}, "cwd": str(REPO_ROOT)}


def test_the_example_registration_is_exec_form_for_questionnaires_only():
    settings = json.loads((SKILL / "assets" / "settings.example.json").read_text(encoding="utf-8"))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "AskUserQuestion"
    (hook,) = entry["hooks"]
    assert hook["command"] == "node"
    assert hook["args"] == ["${CLAUDE_PROJECT_DIR}/.claude/hooks/decision-check.mjs", "docs/owner-decisions.md"]


def test_first_questionnaire_is_denied_with_the_checklist_and_the_retry_passes(tmp_path):
    decision = _run(_ask(), tmp_path)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert EXAMPLE.read_text(encoding="utf-8") in decision["permissionDecisionReason"]
    assert _run(_ask(), tmp_path) is None
    assert _run(_ask(), tmp_path)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_checklist_can_come_from_the_environment_or_an_absolute_path(tmp_path):
    by_env = _run(_ask("s1"), tmp_path, args=(), env_extra={"DECISION_CHECKLIST": EXAMPLE_ARG})
    assert by_env["hookSpecificOutput"]["permissionDecision"] == "deny"
    by_path = _run(_ask("s2"), tmp_path, args=(str(EXAMPLE),))
    assert EXAMPLE.read_text(encoding="utf-8") in by_path["hookSpecificOutput"]["permissionDecisionReason"]


def test_sessions_are_checked_independently(tmp_path):
    assert _run(_ask("s1"), tmp_path) is not None
    assert _run(_ask("s2"), tmp_path) is not None
    assert _run(_ask("s1"), tmp_path) is None


def test_a_retry_after_the_window_is_checked_again(tmp_path):
    state = tmp_path / "decision-check"
    state.mkdir()
    (state / "s1.json").write_text(json.dumps({"denied_at": (time.time() - 11 * 60) * 1000}), encoding="utf-8")
    assert _run(_ask(), tmp_path)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_other_tools_pass_untouched(tmp_path):
    assert _run({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}}, tmp_path) is None
    assert not (tmp_path / "decision-check").exists()


@pytest.mark.parametrize("broken", ["checklist", "state"])
def test_a_failure_lets_the_questionnaire_through_and_says_why(tmp_path, broken):
    project, state = REPO_ROOT, tmp_path
    if broken == "checklist":
        project = tmp_path / "empty-project"
        project.mkdir()
    else:
        state = tmp_path / "a-file"
        state.write_text("not a directory", encoding="utf-8")
    out = _run(_ask(), state, project)
    assert "hookSpecificOutput" not in out
    assert "questionnaire sent unchecked" in out["systemMessage"]
