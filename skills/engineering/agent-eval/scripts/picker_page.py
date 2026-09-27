#!/usr/bin/env python3
"""Render a pick-your-cases page from a candidates JSON file.

candidates.json: {"pick": 3, "candidates": [{"id", "title", "task", "wrong", "fix",
"right", "tags": [...], "source", "snapshot", "rec": bool, "note"}]}

    python picker_page.py candidates.json --out picker.html [--config agent-eval/config.json]

The page speaks the language of the config's `text` file; without a config it is English.
"""
import argparse
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_kit():
    spec = importlib.util.spec_from_file_location("evalkit", HERE / "evalkit.py")
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    return kit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", help="eval config whose `text` sets the page's language")
    ap.add_argument("--title")
    args = ap.parse_args()
    kit = load_kit()
    if args.config:
        kit.configure(args.config)
    data = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    template = kit.fill_text((HERE.parent / "assets" / "picker_template.html").read_text(encoding="utf-8"))
    page = template.replace("__TITLE__", args.title or kit.T["picker.title"])
    page = page.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    Path(args.out).write_text(page, encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
