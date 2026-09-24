#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Find moments where the owner corrected an agent, from Claude Code and Codex session logs.

Whether a message corrects an agent is a judgment about meaning, so no keyword list
decides it. `extract` writes every message the owner typed after an agent turn, with
the end of that turn; only structure is dropped (tool results, command wrappers and
injected reminders are not the owner's words). `classify` has a small, cheap model
label each message: the owner's own words or not, a correction or not, what kind,
what went wrong. `leads` lists the corrections.

    python mine_corrections.py extract --claude-project ~/.claude/projects/-Users-me-myrepo \
        [--codex-home ~/.codex --codex-cwd ~/projects/myrepo] [--since 2026-09-01] [--model fable] \
        --out messages.jsonl
    python mine_corrections.py classify --messages messages.jsonl --labels labels.jsonl \
        [--classifier claude:claude-haiku-4-5] [--batch 20] [--parallel 4] [--budget 20]
    python mine_corrections.py leads --messages messages.jsonl --labels labels.jsonl

`classify` resumes: a rerun labels only the messages without a label. A lead is not
a case: read around it and trace the task back to its start before writing one.
"""
import argparse
import concurrent.futures
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SHA_HINT = re.compile(r"(?:origin/master@|HEAD is now at |checkout: moving from \S+ to )([0-9a-f]{7,40})")
# Structural, not semantic: text a client injects into the user role (commands, reminders,
# another session's message, the interrupt marker).
SKIP_PREFIX = ("<command-", "<local-command", "<system-reminder>", "<task-notification>", "Caveat:",
               "Another Claude session sent a message:", "[Request interrupted by user")
KEEP_OWNER, KEEP_AGENT, KEEP_PREVIOUS = 4000, 1500, 600
PROMPT_OWNER, PROMPT_AGENT = 1500, 1200
KINDS = ("judgment", "instruction", "fact", "style", "none")

CLASSIFY_PROMPT = """Each item below is a message a repository owner typed right after a coding agent's turn, with the end of that turn and the owner's previous message. Label every item:

- from_owner: false when the text is not the owner's own words: a prompt written by another agent or a script, or pasted material (a handoff, log, document, error output) with no instruction of the owner's own.
- correction: true when the owner pushes back on what the agent did, said, planned or decided. Owners often do this with a pointed question: "why not do it now?", "isn't that already cached?", "does this really need a new table?". Read the question against the agent's turn: when it doubts something the agent stated, assumed or chose, it is a correction. A neutral request for information, a new task, approval or a status query is not.
- kind: judgment (the agent's reasoning or decision was wrong: root cause, design, placement, skipped verification, waiting instead of acting, scope), instruction (it did not follow an explicit instruction or rule), fact (it stated something false about code, data or state), style (wording, format, language, tone), none (not a correction).
- code: true when the correction is about code the agent wrote or changed.
- error_type: a few words naming the agent's mistake, for example "wrong root cause", "waited instead of acting", "masked the error"; empty when not a correction.
- summary: one sentence: what the agent did and what the owner wants instead; empty when not a correction.

Messages may be in any language; write error_type and summary in English. Return one entry for every id.

Items:
"""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "from_owner": {"type": "boolean"},
            "correction": {"type": "boolean"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "code": {"type": "boolean"},
            "error_type": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["id", "from_owner", "correction", "kind", "code", "error_type", "summary"],
        "additionalProperties": False,
    }}},
    "required": ["items"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------- extract

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
        # Subagent turns, compaction summaries and injected context are not the owner typing.
        injected = any(row.get(k) for k in ("isSidechain", "isCompactSummary", "isMeta", "isVisibleInTranscriptOnly"))
        role = None if injected else msg.get("role")
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


def owner_messages(path, turns, since="", model_filter=""):
    """Every message the owner typed after an agent turn; nothing is filtered by wording."""
    last_agent, sha, previous, n = None, None, "", 0
    for t in turns:
        if t["hints"]:
            sha = t["hints"][-1]
        if t["role"] == "assistant" and t["text"].strip():
            last_agent = t
            continue
        if t["role"] != "user":
            continue
        text = t["text"].strip()
        if not text or text.startswith(SKIP_PREFIX):
            continue
        n += 1
        keep = last_agent is not None and not (since and t["ts"] < since) and \
            not (model_filter and model_filter not in (last_agent.get("model") or ""))
        if keep:
            yield {"id": f"{Path(path).stem}#{n}", "session": Path(path).stem, "file": str(path),
                   "timestamp": t["ts"], "owner": text[:KEEP_OWNER], "owner_len": len(text),
                   "previous_owner": previous[:KEEP_PREVIOUS], "agent_before": last_agent["text"][-KEEP_AGENT:],
                   "agent_model": last_agent.get("model"), "agent_ts": last_agent["ts"], "last_sha_hint": sha}
        previous = text


def codex_meta(path):
    """The session_meta payload a Codex rollout starts with, or {}."""
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") == "session_meta":
            return row.get("payload") or {}
    return {}


def codex_owner_session(path, prefix):
    """A subagent's rollout holds its parent's prompts, not the owner's words."""
    meta = codex_meta(path)
    source = meta.get("source")
    return not (isinstance(source, dict) and "subagent" in source) and (meta.get("cwd") or "").startswith(prefix)


def sources(claude_projects, codex_home, codex_cwd_prefix=""):
    """Claude Code keeps one folder per project; Codex keeps every project together, so filter by cwd."""
    found = []
    for d in claude_projects:
        found += [(f, claude_turns) for f in sorted(glob.glob(os.path.join(os.path.expanduser(d), "*.jsonl")))]
    if codex_home:
        prefix = os.path.expanduser(codex_cwd_prefix)
        found += [(f, codex_turns) for f in sorted(glob.glob(
            os.path.join(os.path.expanduser(codex_home), "sessions", "**", "rollout-*.jsonl"), recursive=True))
            if codex_owner_session(f, prefix)]
    return found


def cmd_extract(args):
    n = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for path, reader in sources(args.claude_project, args.codex_home, args.codex_cwd):
            for msg in owner_messages(path, reader(path), args.since, args.model):
                out.write(json.dumps(msg, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} owner messages -> {args.out}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ classify

def read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def batch_prompt(batch):
    items = [{"id": m["id"], "previous_owner_message": m.get("previous_owner", "")[:KEEP_PREVIOUS],
              "agent_turn_end": m.get("agent_before", "")[-PROMPT_AGENT:],
              "owner_message": m["owner"][:PROMPT_OWNER] + (" …[truncated]" if m.get("owner_len", 0) > PROMPT_OWNER else "")}
             for m in batch]
    return CLASSIFY_PROMPT + json.dumps(items, ensure_ascii=False, indent=1)


def run_claude(model, prompt, timeout):
    """One tool-less call with a minimal context; returns (parsed JSON, cost in USD).

    Thinking stays on: with it off, recall on known corrections fell from 4/4 to 1/4,
    because telling a pointed question from a neutral one needs the agent's turn weighed.
    """
    argv = [os.environ.get("CLAUDE_BIN", "claude"), "-p", "--model", model, "--restricted", "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--system-prompt", "You label messages and answer only in the requested JSON.",
            "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--no-session-persistence"]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir())
    out = json.loads(proc.stdout or "{}")
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude: {str(out.get('result') or proc.stderr)[:300]}")
    return out["structured_output"], out.get("total_cost_usd")


def run_codex(model, prompt, timeout):
    """One read-only, ephemeral call; Codex reports no cost here, so cost is None."""
    with tempfile.TemporaryDirectory() as tmp:
        schema, last = Path(tmp) / "schema.json", Path(tmp) / "last.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        argv = [os.environ.get("CODEX_BIN", "codex"), "exec", "--model", model, "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                "--output-schema", str(schema), "-o", str(last), "-C", tmp, "-"]
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
        if not last.exists():
            raise RuntimeError(f"codex: {proc.stderr[-300:]}")
        return json.loads(last.read_text(encoding="utf-8")), None


def label_batch(call, batch, classifier):
    """Labels for the ids the model returned that were asked; any other id is ignored, a missing one stays unlabelled."""
    result, cost = call(batch_prompt(batch))
    asked = {m["id"] for m in batch}
    labels = []
    for item in result.get("items", []):
        if item.get("id") in asked and item.get("kind") in KINDS:
            asked.discard(item["id"])
            labels.append({**{k: item[k] for k in SCHEMA["properties"]["items"]["items"]["required"]},
                           "classifier": classifier})
    return labels, cost, sorted(asked)


def cmd_classify(args):
    runtime, _, model = args.classifier.partition(":")
    runners = {"claude": run_claude, "codex": run_codex}
    if runtime not in runners or not model:
        raise SystemExit("--classifier is claude:<model> or codex:<model>")
    done = {row["id"] for row in read_jsonl(args.labels)}
    todo = [m for m in read_jsonl(args.messages) if m["id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"{len(done)} already labelled, {len(todo)} to label in {len(batches)} calls", file=sys.stderr)
    call = lambda prompt: runners[runtime](model, prompt, args.timeout)  # noqa: E731
    lock, spent, missing, failed = threading.Lock(), [0.0], [], [0]
    stop = threading.Event()

    def work(batch):
        if stop.is_set():
            return
        try:
            labels, cost, left = label_batch(call, batch, args.classifier)
        except Exception as exc:  # a failed call leaves its messages unlabelled for the next run
            with lock:
                failed[0] += 1
                print(f"call failed ({len(batch)} messages): {exc}", file=sys.stderr)
            return
        with lock:
            with open(args.labels, "a", encoding="utf-8") as out:
                for row in labels:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
            missing.extend(left)
            spent[0] += cost or 0
            if args.budget and spent[0] >= args.budget:
                stop.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        list(pool.map(work, batches))
    print(f"spent ${spent[0]:.2f}; {failed[0]} failed calls; {len(missing)} messages the model skipped"
          + ("; stopped at budget" if stop.is_set() else "") + ". Rerun to label what is left.", file=sys.stderr)
    return 0


# --------------------------------------------------------------------- leads

def cmd_leads(args):
    labels = {row["id"]: row for row in read_jsonl(args.labels)}
    kinds = set(args.kinds.split(","))
    counts = {}
    rows = []
    for m in read_jsonl(args.messages):
        lab = labels.get(m["id"])
        if lab is None:
            continue
        key = lab["kind"] if lab["correction"] and lab["from_owner"] else "not a correction"
        counts[key] = counts.get(key, 0) + 1
        if lab["correction"] and lab["from_owner"] and lab["kind"] in kinds:
            rows.append({**{k: m[k] for k in ("id", "session", "timestamp", "owner", "agent_model", "last_sha_hint")},
                         "agent_before": m["agent_before"][-400:], **{k: lab[k] for k in ("kind", "code", "error_type", "summary")}})
    for row in sorted(rows, key=lambda r: r["timestamp"]):
        print(json.dumps(row, ensure_ascii=False))
    print(f"labelled {len(labels)}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="write every owner message after an agent turn")
    ex.add_argument("--claude-project", action="append", default=[])
    ex.add_argument("--codex-home")
    ex.add_argument("--codex-cwd", default="", help="keep Codex sessions whose working directory starts with this")
    ex.add_argument("--since", default="")
    ex.add_argument("--model", default="", help="substring of the corrected agent's model id")
    ex.add_argument("--out", required=True)
    cl = sub.add_parser("classify", help="label each message with a small model")
    cl.add_argument("--messages", required=True)
    cl.add_argument("--labels", required=True)
    cl.add_argument("--classifier", default="claude:claude-haiku-4-5", help="claude:<model> or codex:<model>")
    cl.add_argument("--batch", type=int, default=20)
    cl.add_argument("--parallel", type=int, default=4)
    cl.add_argument("--timeout", type=int, default=300)
    cl.add_argument("--budget", type=float, default=0, help="stop launching calls once this many dollars are spent")
    cl.add_argument("--limit", type=int, default=0, help="label at most this many messages this run")
    le = sub.add_parser("leads", help="list the messages labelled as corrections")
    le.add_argument("--messages", required=True)
    le.add_argument("--labels", required=True)
    le.add_argument("--kinds", default="judgment,instruction,fact")
    args = ap.parse_args(argv)
    return {"extract": cmd_extract, "classify": cmd_classify, "leads": cmd_leads}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
