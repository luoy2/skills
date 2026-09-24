"""agent-eval case mining: the deterministic parts around the model's judgment.

Whether a message corrects an agent is left to a small model, so extraction must
hand it every owner message and nothing else: a wording filter would decide in
its place, and injected text (compaction summaries, reminders, subagent prompts)
would be judged as the owner's words. Labels must line up with the ids asked, and
a rerun must label only what is still unlabelled.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MINE_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "mine_corrections.py"


@pytest.fixture(scope="module")
def mine():
    spec = importlib.util.spec_from_file_location("mine_corrections_under_test", MINE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _claude(role, text, **flags):
    return json.dumps({"timestamp": "2026-01-01T00:00:00Z", "message": {"role": role, "content": text}, **flags})


def test_extraction_keeps_every_owner_message_and_drops_only_injected_text(mine, tmp_path):
    path = tmp_path / "s1.jsonl"
    rows = [
        _claude("user", "start the job"),
        _claude("assistant", "I will wait for the 15:45 slot."),
        _claude("user", "ok thanks"),
        _claude("user", [{"type": "tool_result", "content": "exit 0"}]),
        _claude("user", "<system-reminder>context</system-reminder>"),
        _claude("user", "Another Claude session sent a message:\n<teammate-message>done</teammate-message>"),
        _claude("user", "[Request interrupted by user]"),
        _claude("user", "Summary of the earlier conversation", isCompactSummary=True),
        _claude("user", "skill body", isMeta=True),
        _claude("assistant", "Done."),
        _claude("user", "why not now?"),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    got = list(mine.owner_messages(path, mine.claude_turns(path)))
    assert [m["owner"] for m in got] == ["ok thanks", "why not now?"]
    assert got[1]["previous_owner"] == "ok thanks"
    assert got[1]["agent_before"] == "Done."
    assert len({m["id"] for m in got}) == 2


def test_labels_line_up_with_the_ids_asked(mine):
    batch = [{"id": "a", "owner": "x"}, {"id": "b", "owner": "y"}]
    reply = {"items": [
        {"id": "a", "from_owner": True, "correction": True, "kind": "judgment", "code": False,
         "error_type": "waited", "summary": "s"},
        {"id": "z", "from_owner": True, "correction": True, "kind": "judgment", "code": False,
         "error_type": "", "summary": ""},
    ]}
    labels, cost, missing = mine.label_batch(lambda prompt: (reply, 0.01), batch, "claude:m")
    assert [row["id"] for row in labels] == ["a"]
    assert missing == ["b"]
    assert cost == 0.01


def test_a_rerun_labels_only_what_is_unlabelled(mine, monkeypatch, tmp_path):
    messages, labels = tmp_path / "m.jsonl", tmp_path / "l.jsonl"
    messages.write_text("".join(json.dumps({"id": i, "owner": i, "owner_len": 1}) + "\n" for i in "abc"),
                        encoding="utf-8")
    labels.write_text(json.dumps({"id": "a", "from_owner": True, "correction": False, "kind": "none"}) + "\n",
                      encoding="utf-8")
    asked = []

    def fake(model, prompt, timeout):
        items = json.loads(prompt[prompt.index("["):])
        asked.extend(item["id"] for item in items)
        return {"items": [{"id": item["id"], "from_owner": True, "correction": False, "kind": "none",
                           "code": False, "error_type": "", "summary": ""} for item in items]}, 0.0

    monkeypatch.setattr(mine, "run_claude", fake)
    mine.main(["classify", "--messages", str(messages), "--labels", str(labels), "--parallel", "1"])
    assert sorted(asked) == ["b", "c"]
    assert sorted(json.loads(line)["id"] for line in labels.read_text().splitlines()) == ["a", "b", "c"]


def test_codex_sessions_are_kept_by_working_directory_and_subagents_are_skipped(mine, tmp_path):
    day = tmp_path / "codex" / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)

    def rollout(name, **meta):
        (day / f"rollout-{name}.jsonl").write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n",
                                                   encoding="utf-8")

    rollout("mine", cwd="/work/repo")
    rollout("worktree", cwd="/work/repo-feature")
    rollout("other", cwd="/work/elsewhere")
    rollout("sub", cwd="/work/repo", source={"subagent": {"other": "guardian"}})
    got = sorted(Path(f).name for f, _ in mine.sources([], str(tmp_path / "codex"), "/work/repo"))
    assert got == ["rollout-mine.jsonl", "rollout-worktree.jsonl"]
