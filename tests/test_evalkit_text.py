"""agent-eval text table: every string a candidate, the reviewer, a judge or the owner reads.

A text file replaces the whole table, for example with a translation; a missing or unknown
entry is an error, so one language never shows up inside another. Candidate, reviewer and
judge entries are experiment inputs: every ledger row records their digest, and a batch
refuses new rows on another text, so one batch never compares candidates across two prompts.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "engineering" / "agent-eval"
KIT_PATH = SKILL / "scripts" / "evalkit.py"


def fresh_kit():
    spec = importlib.util.spec_from_file_location("evalkit_text_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _marked(kit):
    """The default table with every string prefixed: placeholders keep working, output is recognisable."""
    return {k: (v if isinstance(v, list) else "X·" + v) for k, v in kit.TEXT_EN.items()}


def _config(tmp_path, text=None):
    config = json.loads((SKILL / "assets" / "config.example.json").read_text(encoding="utf-8"))
    config.update({"repo": str(tmp_path), "cases_dir": str(tmp_path / "cases"), "results_dir": str(tmp_path / "results")})
    if text is not None:
        (tmp_path / "text.json").write_text(json.dumps(text, ensure_ascii=False), encoding="utf-8")
        config["text"] = "text.json"  # relative to the config file, like every other path in it
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_a_text_file_replaces_every_entry_and_the_prompts_use_it(tmp_path):
    kit = fresh_kit()
    kit.configure(_config(tmp_path, _marked(kit)))
    case = {"background": ["a fact"], "owner_messages": ["do it"], "attachments": []}
    prompt = kit.build_prompt(case, {"suffix": "SUFFIX"})
    assert prompt.startswith("X·Background:\n1. a fact\n\nX·The owner (verbatim, in order):\nX·> do it")
    assert kit.adopt_prompt_extra("D", "R").startswith("X·\n\n=== Previous round ===")
    assert kit.TEXT_DIGEST != fresh_kit().TEXT_DIGEST


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_a_missing_or_unknown_entry_is_an_error(tmp_path, change):
    kit = fresh_kit()
    text = dict(kit.TEXT_EN)
    if change == "missing":
        del text["judge.no_steps"]
    else:
        text["judge.no_step"] = "typo"
    with pytest.raises(kit.EvalError, match=r"judge\.no_step"):
        kit.configure(_config(tmp_path, text))


def test_only_what_candidates_reviewer_and_judges_read_changes_the_digest():
    kit = fresh_kit()
    base = kit.text_digest(kit.TEXT_EN)
    assert kit.text_digest({**kit.TEXT_EN, "report.title": "Another title {batch}"}) == base
    assert kit.text_digest({**kit.TEXT_EN, "casepage.lede": "another lede"}) == base
    for key in ("candidate.owner", "reviewer.prompt", "judge.no_steps"):
        assert kit.text_digest({**kit.TEXT_EN, key: "changed"}) != base


def test_every_ledger_row_records_its_text_and_recompute_rewrites_keep_theirs(tmp_path):
    kit = fresh_kit()
    kit.RecordWriter(tmp_path / "runs.jsonl").append({"run_id": "a"})
    kit.RecordWriter(tmp_path / "old.jsonl", stamp=False).append({"run_id": "b"})
    (new,) = kit.read_jsonl(tmp_path / "runs.jsonl")
    (rewritten,) = kit.read_jsonl(tmp_path / "old.jsonl")
    assert new["text_digest"] == kit.TEXT_DIGEST and new["kit_digest"] == kit.KIT_DIGEST
    assert "text_digest" not in rewritten


@pytest.mark.parametrize("ledger", ["runs.jsonl", "plans.jsonl", "judgments.jsonl", "calibration.jsonl"])
def test_a_batch_written_on_another_text_refuses_new_rows(tmp_path, monkeypatch, ledger):
    kit = fresh_kit()
    monkeypatch.setattr(kit, "RESULTS", tmp_path)
    kit.check_batch_text("empty")  # a new batch takes any text
    same = tmp_path / "same" / ledger
    kit.RecordWriter(same).append({"run_id": "a"})
    kit.check_batch_text("same")
    for batch, row in (("other", {"run_id": "a", "text_digest": "0123456789ab"}), ("before", {"run_id": "a"})):
        kit.RecordWriter(tmp_path / batch / ledger, stamp=False).append(row)
        with pytest.raises(kit.EvalError, match="0123456789ab" if batch == "other" else "none"):
            kit.check_batch_text(batch)


def test_owner_pages_speak_the_configured_language(tmp_path):
    kit = fresh_kit()
    config = _config(tmp_path, _marked(kit))
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"pick": 1, "candidates": [{"id": "c1", "title": "t", "task": "x", "wrong": "y",
                                                                  "fix": "%%not.a.key%%", "right": "z"}]}),
                          encoding="utf-8")
    picker = SKILL / "scripts" / "picker_page.py"
    for extra, lede in (([], kit.TEXT_EN["picker.lede"]), (["--config", str(config)], "X·" + kit.TEXT_EN["picker.lede"])):
        out = tmp_path / "picker.html"
        subprocess.run([sys.executable, str(picker), str(candidates), "--out", str(out), *extra], check=True,
                       capture_output=True)
        page = out.read_text(encoding="utf-8")
        assert f'<p class="lede">{lede}</p>' in page
        assert "%%not.a.key%%" in page  # case data is never read as a placeholder
        assert page.count("%%") == 2


def test_the_text_command_prints_the_table_to_translate(tmp_path):
    kit = fresh_kit()
    out = subprocess.run([sys.executable, str(KIT_PATH), "--config", str(tmp_path / "absent.json"), "text"],
                         check=True, capture_output=True, text=True).stdout
    assert json.loads(out) == kit.TEXT_EN
