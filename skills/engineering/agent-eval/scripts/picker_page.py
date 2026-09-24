#!/usr/bin/env python3
"""Render a pick-your-cases page from a candidates JSON file.

candidates.json: {"pick": 3, "candidates": [{"id", "title", "task", "wrong", "fix",
"right", "tags": [...], "source", "snapshot", "rec": bool, "note"}]}

    python picker_page.py candidates.json --out picker.html
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="Pick eval cases")
    args = ap.parse_args()
    data = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    template = (HERE.parent / "assets" / "picker_template.html").read_text(encoding="utf-8")
    page = template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    Path(args.out).write_text(page.replace("__TITLE__", args.title), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
