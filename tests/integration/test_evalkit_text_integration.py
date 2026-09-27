"""agent-eval text table cases that run a script in a child interpreter: the owner pages
speak the configured language, and the `text` command prints the table to translate. They
live under tests/integration/; tests/test_evalkit_text.py states the rules of the table.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
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
