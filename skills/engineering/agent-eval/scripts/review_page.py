#!/usr/bin/env python3
"""Render an annotatable review page for the owner from a cases directory.

Each case tab shows exactly what the candidate will see (the built prompt), the
two-level Trap, every rubric item with its evidence source, equivalents, leak
markers and the case checks (`evalkit.py lint-case`). The owner marks each item
agree / change / drop and copies the result back into the conversation.

    python review_page.py --config agent-eval/config.json --out review.html [--cases I,A]
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
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases")
    ap.add_argument("--title")
    args = ap.parse_args()
    kit = load_kit()
    kit.configure(args.config)
    common, cases = kit.load_cases(args.cases.split(",") if args.cases else None)
    data = {"key": str(kit.CASES), "common": common, "cases": [],
            "impl_modes": (kit.CFG.get("implement") or {}).get("modes", []),
            "lint_labels": {k[len("lint."):]: v for k, v in kit.T.items() if k.startswith("lint.")}}
    for case in cases.values():
        row = {k: v for k, v in case.items() if k != "dir"}
        row["prompt"] = kit.build_prompt(case, common)
        try:
            row["lint"] = kit.lint_case(case, common)
        except kit.EvalError as exc:  # a snapshot missing from this checkout, for example
            row["lint_error"] = str(exc)
        data["cases"].append(row)
    template = kit.fill_text((HERE.parent / "assets" / "review_template.html").read_text(encoding="utf-8"))
    page = template.replace("__TITLE__", args.title or kit.T["casepage.title"])
    page = page.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    Path(args.out).write_text(page, encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
