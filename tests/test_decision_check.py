"""decision-check: the questionnaire hook a project registers in .claude/settings.json.

Each questionnaire is denied once with the project's checklist as the reason, and
the model's retry from the same session goes through; other tools are untouched,
and a failure lets the question through rather than keep the owner from being
asked. Running the hook starts node, so those cases are in
tests/integration/test_decision_check_integration.py; this file keeps the registration example.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "engineering" / "questionnaire-review"


def test_the_example_registration_is_exec_form_for_questionnaires_only():
    settings = json.loads((SKILL / "assets" / "settings.example.json").read_text(encoding="utf-8"))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "AskUserQuestion"
    (hook,) = entry["hooks"]
    assert hook["command"] == "node"
    assert hook["args"] == ["${CLAUDE_PROJECT_DIR}/.claude/hooks/decision-check.mjs", "docs/owner-decisions.md"]
