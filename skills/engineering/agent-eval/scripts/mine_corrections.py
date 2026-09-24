#!/usr/bin/env python3
"""Find moments where the owner corrected an agent, from Claude Code and Codex session logs.

Prints one JSON line per hit: the owner's message, the agent turn just before it
(with its model), and any checkout SHA the session printed earlier. A hit is a
lead, not a case: read around it, confirm the correction was about judgment
(not a typo), and trace the task back to its start before writing anything.

    python mine_corrections.py --claude-project ~/.claude/projects/-Users-me-myrepo \
        [--codex-home ~/.codex] [--since 2026-09-01] [--model fable] [--limit 50]
"""
import argparse
import glob
import json
import os
import re
from pathlib import Path

DEFAULT_PATTERN = (r"不对|错了|不是吧|不应该|为什么|别这样|停一下|重来|你又|我说的是|怎么能|搞成啥|过时了|"
                   r"\bwrong\b|\bthat's not\b|\bwhy did you\b|\bI said\b|\bstop\b|\bno,")
SHA_HINT = re.compile(r"(?:origin/master@|HEAD is now at |checkout: moving from \S+ to )([0-9a-f]{7,40})")
SKIP_PREFIX = ("<command-", "<local-command", "<system-reminder>", "<task-notification>", "Caveat:")


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"):
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def is_tool_result(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def claude_turns(path):
    for line in open(path, encoding="utf-8", errors="replace"):
        hint = SHA_HINT.findall(line)
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") or {}
        role = msg.get("role")
        yield {"ts": row.get("timestamp", ""), "role": role, "model": msg.get("model"),
               "text": "" if is_tool_result(msg.get("content")) else text_of(msg.get("content")), "hints": hint}


def codex_turns(path):
    model = None
    for line in open(path, encoding="utf-8", errors="replace"):
        hint = SHA_HINT.findall(line)
        try:
            row = json.loads(line)
        except ValueError:
            continue
        p = row.get("payload") or {}
        if row.get("type") == "turn_context":
            model = p.get("model", model)
        if row.get("type") == "response_item" and p.get("type") == "message":
            yield {"ts": row.get("timestamp", ""), "role": p.get("role"), "model": model,
                   "text": text_of(p.get("content")), "hints": hint}
        elif hint:
            yield {"ts": row.get("timestamp", ""), "role": None, "model": model, "text": "", "hints": hint}


def scan(path, turns, pattern, since, model_filter, max_len):
    last_agent, sha = None, None
    for t in turns:
        if t["hints"]:
            sha = t["hints"][-1]
        if t["role"] == "assistant" and t["text"].strip():
            last_agent = t
            continue
        if t["role"] != "user":
            continue
        text = t["text"].strip()
        if not text or text.startswith(SKIP_PREFIX) or len(text) > max_len or (since and t["ts"] < since):
            continue
        if not pattern.search(text) or last_agent is None:
            continue
        if model_filter and model_filter not in (last_agent.get("model") or ""):
            continue
        yield {"session": Path(path).stem, "file": str(path), "timestamp": t["ts"], "owner": text[:600],
               "agent_before": last_agent["text"][-600:], "agent_model": last_agent.get("model"),
               "agent_ts": last_agent["ts"], "last_sha_hint": sha}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-project", action="append", default=[])
    ap.add_argument("--codex-home")
    ap.add_argument("--since", default="")
    ap.add_argument("--model", default="", help="substring of the corrected agent's model id")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN)
    ap.add_argument("--max-len", type=int, default=1500, help="longer owner messages are usually pasted text")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    pattern = re.compile(args.pattern, re.I)
    sources = []
    for d in args.claude_project:
        sources += [(f, claude_turns) for f in sorted(glob.glob(os.path.join(os.path.expanduser(d), "*.jsonl")))]
    if args.codex_home:
        sources += [(f, codex_turns) for f in sorted(glob.glob(
            os.path.join(os.path.expanduser(args.codex_home), "sessions", "**", "rollout-*.jsonl"), recursive=True))]
    n = 0
    for path, reader in sources:
        for hit in scan(path, reader(path), pattern, args.since, args.model, args.max_len):
            print(json.dumps(hit, ensure_ascii=False))
            n += 1
            if n >= args.limit:
                return


if __name__ == "__main__":
    main()
