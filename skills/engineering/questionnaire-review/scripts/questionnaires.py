#!/usr/bin/env -S uv run --locked --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Questionnaires that Claude Code sessions sent the owner, and how the owner answered them.

A recommendation the owner overrode marks a place where the agents' defaults and the
owner's differ. `extract` reads Claude Code session logs and writes one row per
question: the options, which one was marked recommended, and the answer. `classify`
has a small model label what each recommended option does and, for an overridden
one, what the owner chose instead: a judgment about meaning, so no keyword list
decides it. `stats` counts; `leads` prints the overridden questions to read in full.

    questionnaires.py extract --since 2026-01-01T00:00:00Z [--until ...] [--projects ROOT ...] --out rows.jsonl
    questionnaires.py classify --rows rows.jsonl --labels labels.jsonl [--classifier claude:claude-haiku-4-5] [--batch 25] [--parallel 4]
    questionnaires.py stats --rows rows.jsonl [--labels labels.jsonl] [--split 2026-01-15T00:00:00Z]
    questionnaires.py leads --rows rows.jsonl [--labels labels.jsonl] [--tz America/New_York]

Rows carry the owner's words and the agents' text: keep them in a private directory,
never in the repository. `leads` masks secret-shaped strings before printing.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RECOMMENDED = re.compile(r"[(（]\s*(?:recommended|推荐)\s*[)）]\s*$", re.IGNORECASE)
DISMISSED = "[User dismissed"
# A tool error is the owner's refusal only when Claude Code says so. A hook that blocked the
# call before the owner saw it, and a call the session ended before answering, are not the
# owner's choice; any other error is kept apart as `error`.
REJECTED = "The user doesn't want to proceed with this tool use"
HOOK_BLOCKED = re.compile(r"^\s*PreToolUse:\S+ hook\b")
INTERRUPTED = "[Tool call interrupted"
KEEP_TEXT = 1000
SECRET = re.compile(
    r"https?://\S*(?:hook|token|key|secret)\S*"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\b[A-Fa-f0-9]{32,}\b"
    r"|[A-Za-z0-9+/_-]{40,}={0,2}",
    re.IGNORECASE,
)
ACTIONS = ("act_now", "defer", "keep", "remove", "owner_acts", "agent_acts", "file_ticket", "fix_now",
           "split", "combine", "verify_first", "relax_or_waive", "restructure", "other")
THEMES = ("timing", "evidence", "problem_handling", "structure", "legacy", "roles", "granularity",
          "knowledge", "visual", "other", "none")
ANSWERED = ("accepted", "changed", "own")


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def owner_text(entry):
    """What the owner typed in a user entry; empty for tool results and client-injected text."""
    if entry.get("type") != "user":
        return ""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return ""
    text = text_of(content).strip()
    return "" if text.startswith("<") or text.startswith("[Request interrupted") else text


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def kind_of(question, answer, recommended):
    """accepted / changed / own / multi / no_recommendation, plus the picked option indices."""
    labels = [o.get("label", "") for o in question.get("options") or []]
    values = answer if isinstance(answer, list) else [answer]
    picked = sorted(labels.index(v) for v in values if v in labels)
    own = " | ".join(v for v in values if v not in labels)
    if question.get("multiSelect"):
        return "multi", picked, own
    if not picked:
        return "own", [], own
    if not recommended:
        return "no_recommendation", picked, own
    return ("accepted" if picked[0] in recommended else "changed"), picked, own


def calls_in(path, diagnostics=None):
    """Pair official assistant calls with user results; cached events never consume a call."""
    diagnostics = diagnostics if diagnostics is not None else []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("not an event")
        except ValueError:
            diagnostics.append({"code": "invalid_event", "path": str(path), "line": number})
            continue
        entries.append({**entry, "_line": number})
    uses = {}
    for i, entry in enumerate(entries):
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            is_call = block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion"
            is_result = block.get("type") == "tool_result" and block.get("tool_use_id") in uses
            if not (is_call or is_result):
                continue
            expected_type = "assistant" if is_call else "user"
            if entry.get("type") != expected_type:
                diagnostics.append({"code": "ignored_nonofficial_event", "path": str(path), "line": entry["_line"]})
                continue
            if is_call:
                if not block.get("id"):
                    diagnostics.append({"code": "missing_call_id", "path": str(path), "line": entry["_line"]})
                    continue
                before = ""
                for j in range(i, max(-1, i - 8), -1):
                    if entries[j].get("type") == "assistant":
                        before = text_of((entries[j].get("message") or {}).get("content")).strip()
                        if before:
                            break
                    elif owner_text(entries[j]):
                        break
                if block["id"] in uses:
                    previous_entry, previous_block, previous_before = uses[block["id"]]
                    yield previous_entry, previous_block, None, None, previous_before, ""
                uses[block["id"]] = (entry, block, before)
            elif is_result:
                use_entry, use_block, before = uses.pop(block["tool_use_id"])
                after = next((owner_text(e) for e in entries[i + 1:i + 40] if owner_text(e)), "")
                yield use_entry, use_block, entry, block, before, after
    for use_entry, use_block, before in uses.values():
        yield use_entry, use_block, None, None, before, ""


def instant(value):
    """A timezone-aware timestamp, or unknown; never synthesize a log clock."""
    try:
        parsed = parse_ts(value)
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def error_status(content):
    """The status of a questionnaire whose tool result is an error."""
    if "InputValidationError" in content:
        return "invalid"
    if HOOK_BLOCKED.match(content):
        return "hook_blocked"
    if content.lstrip().startswith(INTERRUPTED):
        return "missing"
    return "declined" if REJECTED in content else "error"


def rows_for(use_entry, block, result_entry, result, before, after, project):
    questions = (block.get("input") or {}).get("questions") or []
    status = "missing"
    content = ""
    answers, annotations = {}, {}
    if result is not None:
        content = text_of(result.get("content")) if not isinstance(result.get("content"), str) else result["content"]
        if result.get("is_error"):
            status = error_status(content)
        else:
            status = "answered"
            extra = result_entry.get("toolUseResult") if isinstance(result_entry.get("toolUseResult"), dict) else {}
            answers, annotations = extra.get("answers") or {}, extra.get("annotations") or {}
    seconds = None
    if result_entry is not None and len(questions) == 1:
        start, end = instant(use_entry.get("timestamp")), instant(result_entry.get("timestamp"))
        if start is not None and end is not None and end >= start:
            seconds = (end - start).total_seconds()
    for qi, question in enumerate(questions):
        options = [{"label": o.get("label", ""), "description": o.get("description", "")} for o in question.get("options") or []]
        recommended = [i for i, o in enumerate(options) if RECOMMENDED.search(o["label"])]
        answer = answers.get(question.get("question"))
        if answer is None and status == "answered":
            match = re.search(re.escape(f'"{question.get("question")}"="') + r'(.*?)"(?:, "|\. | selected| user notes|$)', content, re.S)
            answer = match.group(1) if match else None
        row_status = status
        if isinstance(answer, str) and answer.startswith(DISMISSED):
            row_status = "declined"
        if row_status == "answered" and answer is not None:
            kind, picked, own = kind_of(question, answer, recommended)
        else:
            kind, picked, own = (row_status if row_status != "answered" else "missing"), [], ""
        yield {
            "id": f"{block['id']}#{qi}", "call": block["id"], "q": qi, "questions_in_call": len(questions),
            "ts": use_entry.get("timestamp"), "session": use_entry.get("sessionId"), "project": project,
            "header": question.get("header", ""), "question": question.get("question", ""), "options": options,
            "multi": bool(question.get("multiSelect")), "recommended": recommended, "status": row_status,
            "answer": answer, "notes": (annotations.get(question.get("question")) or {}).get("notes"),
            "kind": kind, "picked": picked, "own_text": own, "seconds": seconds,
            "before": before[-KEEP_TEXT:], "after": after[:KEEP_TEXT],
        }


def cmd_extract(args):
    since, until = instant(args.since), instant(args.until) if args.until else None
    if since is None or (args.until and (until is None or until <= since)):
        raise SystemExit("--since/--until require timezone-aware timestamps with until after since")
    roots = sorted({Path(p).expanduser().resolve() for p in (args.projects or ["~/.claude/projects"])})
    report = {"channel": "AskUserQuestion", "since": args.since, "until": args.until,
              "roots": [], "diagnostics": [], "duplicate_calls": 0, "conflicting_calls": 0}
    diagnostics, candidates = report["diagnostics"], {}
    for root in roots:
        coverage = {"root": str(root), "status": "read", "files": 0, "read_files": 0}
        report["roots"].append(coverage)
        if not root.is_dir():
            coverage["status"] = "missing" if not root.exists() else "unreadable"
            diagnostics.append({"code": "unreadable_root", "path": str(root)})
            continue
        def walk_error(error):
            coverage["status"] = "partial"
            diagnostics.append({"code": "unreadable_source", "path": str(error.filename), "errno": error.errno})
        for directory, dirs, files in os.walk(root, onerror=walk_error):
            dirs.sort()
            for name in sorted(files):
                if not name.endswith(".jsonl"):
                    continue
                path = Path(directory) / name
                coverage["files"] += 1
                try:
                    calls = list(calls_in(path, diagnostics))
                except OSError as error:
                    coverage["status"] = "partial"
                    diagnostics.append({"code": "unreadable_source", "path": str(path), "errno": error.errno})
                    continue
                coverage["read_files"] += 1
                project = path.relative_to(root).parts[0]
                for use, block, result_entry, result, before, after in calls:
                    timestamp = instant(use.get("timestamp"))
                    source = {"root": str(root), "path": str(path), "call_line": use["_line"],
                              "result_line": result_entry["_line"] if result_entry else None}
                    if timestamp is None:
                        diagnostics.append({"code": "invalid_call_timestamp", "call": block["id"], **source})
                        continue
                    if timestamp < since or (until and timestamp >= until):
                        continue
                    rows = list(rows_for(use, block, result_entry, result, before, after, project))
                    if not rows:
                        diagnostics.append({"code": "missing_questions", "call": block["id"], **source})
                        continue
                    if result_entry and len(rows) == 1 and rows[0]["seconds"] is None:
                        diagnostics.append({"code": "unknown_answer_time", "call": block["id"], **source})
                    # Paths and surrounding prose can change in a resumed copy; the call and answer cannot.
                    identity = [{k: v for k, v in row.items() if k not in ("project", "before", "after")}
                                for row in rows]
                    key = json.dumps(identity, sort_keys=True, ensure_ascii=False)
                    candidates.setdefault(block["id"], []).append((key, rows, source))
    rows = []
    for call, copies in sorted(candidates.items()):
        if len({key for key, _, _ in copies}) > 1:
            report["conflicting_calls"] += 1
            diagnostics.append({"code": "conflicting_copy", "call": call,
                                "sources": [source for _, _, source in copies]})
            continue
        report["duplicate_calls"] += len(copies) - 1
        rows.extend({**row, "sources": [source for _, _, source in copies]} for row in copies[0][1])
    rows.sort(key=lambda r: (instant(r["ts"]), r["call"], r["q"]))
    report.update(questionnaires=len({r["call"] for r in rows}), questions=len(rows))
    report["complete"] = not any(d["code"] not in ("ignored_nonofficial_event", "unknown_answer_time") for d in diagnostics)
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    Path(str(args.out) + ".coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['questionnaires']} questionnaires, {len(rows)} questions -> {args.out}")
    print_coverage(args.out)
    if not report["complete"]:
        raise SystemExit(1)


def print_coverage(rows_path):
    path = Path(str(rows_path) + ".coverage.json")
    if not path.exists():
        print("extraction coverage: unknown (no companion report)")
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    read = sum(r["status"] == "read" for r in report["roots"])
    print(f"extraction coverage: {read}/{len(report['roots'])} roots read; "
          f"duplicate calls {report['duplicate_calls']}; conflicting calls {report['conflicting_calls']}; "
          f"complete {report['complete']} (declared AskUserQuestion sources only)")


CLASSIFY_PROMPT = """Each item is one question a coding agent put to a repository owner as a multiple-choice questionnaire, with the option the agent marked recommended and the owner's answer (a listed option, or the owner's own words). Label every item.

- rec_action: what the recommended option does, named by what sets it apart from the other options in the same question. act_now (do it now), defer (wait for a later time or event), keep (leave the current thing as it is), remove (delete or retire something), owner_acts (the owner performs a hands-on step: log in, click, paste a key, run a command; approving or deciding is not owner_acts), agent_acts (the agent performs with tools or credentials a step the owner could do by hand), file_ticket (record it as a new issue for later), fix_now (fix it within the current work), split (separate PRs, tickets or steps), combine (one PR, ticket or batch), verify_first (review, reproduce or check before acting), relax_or_waive (waive, lower or reword a criterion or gate), restructure (change the design or structure), other.
- chosen_action: the same list, for what the owner chose. Equal to rec_action when the owner took the recommended option.
- theme: when the owner did not take the recommendation, what the difference is about: timing (when to act), evidence (how to prove behaviour), problem_handling (file a ticket, note it or fix it now; root cause versus patch), structure (design, abstraction or placement), legacy (keep or remove existing code, documents, services or concepts), roles (who does the work, including the owner doing a step by hand), granularity (splitting or batching PRs, tickets, steps or decisions), knowledge (the question rested on a stale or wrong fact, or the owner asked what something means or why), visual (look and layout), other (none of these). Use none only when the owner took the recommended option.

Option labels are often written in the owner's voice: in "I'll add it now" or "我现在加", I/我 is the owner and you/你 the agent. When the agent speaks ("I'll watch it at 08:30 tomorrow", "明早 08:30，我盯着"), I/我 is the agent. Decide from the question which party would do the work before choosing owner_acts or agent_acts.
- note: one sentence on what the owner wanted instead of the recommendation; empty when the owner took it.

Questions and answers may be in any language; write the note in English. Return one entry for every id.

Items:
"""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "rec_action": {"type": "string", "enum": list(ACTIONS)},
            "chosen_action": {"type": "string", "enum": list(ACTIONS)},
            "theme": {"type": "string", "enum": list(THEMES)},
            "note": {"type": "string"},
        },
        "required": ["id", "rec_action", "chosen_action", "theme", "note"],
        "additionalProperties": False,
    }}},
    "required": ["items"],
    "additionalProperties": False,
}


def batch_prompt(batch):
    items = []
    for row in batch:
        answer = row["answer"] if isinstance(row["answer"], str) else " | ".join(row["answer"] or [])
        items.append({
            "id": row["id"], "question": row["question"][:600],
            "options": [{"label": o["label"][:160], "description": o["description"][:300]} for o in row["options"]],
            "recommended_option": row["options"][row["recommended"][0]]["label"][:160],
            "owner_answer": answer[:600], "owner_took_recommendation": row["kind"] == "accepted",
        })
    return CLASSIFY_PROMPT + json.dumps(items, ensure_ascii=False, indent=1)


def run_claude(model, prompt, timeout):
    """One tool-less call with a minimal context; returns (parsed JSON, cost in USD)."""
    argv = [os.environ.get("CLAUDE_BIN", "claude"), "-p", "--model", model, "--restricted", "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--system-prompt", "You label questionnaire answers and answer only in the requested JSON.",
            "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--no-session-persistence"]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir())
    out = json.loads(proc.stdout or "{}")
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude: {str(out.get('result') or proc.stderr)[:300]}")
    return out["structured_output"], out.get("total_cost_usd")


def row_digest(row):
    fields = ("id", "session", "ts", "question", "options", "recommended", "answer", "notes", "kind", "multi")
    body = json.dumps({key: row.get(key) for key in fields}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def current_labels(rows, path):
    """Legacy or changed-answer labels are unbound, even when the call id was reused."""
    digests = {row["id"]: row_digest(row) for row in rows}
    return {label["id"]: label for label in read_jsonl(path)
            if label.get("row_digest") and label["row_digest"] == digests.get(label["id"])}


def label_batch(call, batch, classifier):
    """Labels for the ids asked; an id the model skipped stays unlabelled for the next run."""
    result, cost = call(batch_prompt(batch))
    asked = {row["id"] for row in batch}
    digests = {row["id"]: row_digest(row) for row in batch}
    labels = []
    for item in result.get("items", []):
        if item.get("id") in asked:
            asked.discard(item["id"])
            labels.append({**{k: item[k] for k in SCHEMA["properties"]["items"]["items"]["required"]},
                           "classifier": classifier, "row_digest": digests[item["id"]]})
    return labels, cost, sorted(asked)


def cmd_classify(args):
    runtime, _, model = args.classifier.partition(":")
    if runtime != "claude" or not model:
        raise SystemExit("--classifier is claude:<model>")
    rows = read_jsonl(args.rows)
    done = current_labels(rows, args.labels)
    todo = [r for r in rows if r["kind"] in ANSWERED and r["recommended"] and not r["multi"] and r["id"] not in done]
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    spent, missing, failed = 0.0, [], 0
    call = lambda prompt: run_claude(model, prompt, args.timeout)  # noqa: E731
    with open(args.labels, "a", encoding="utf-8") as out, concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
        pending = {pool.submit(label_batch, call, b, args.classifier): b for b in batches}
        for future in concurrent.futures.as_completed(pending):
            try:
                labels, cost, skipped = future.result()
            except Exception as error:  # the whole batch stays unlabelled; a rerun retries it
                print(f"batch failed: {error}", file=sys.stderr)
                failed += 1
                missing += [row["id"] for row in pending[future]]
                continue
            out.writelines(json.dumps(label, ensure_ascii=False) + "\n" for label in labels)
            out.flush()
            spent += cost or 0.0
            missing += skipped
    failures = f" ({failed} of {len(batches)} batches failed)" if failed else ""
    print(f"labelled {len(todo) - len(missing)} of {len(todo)}; cost ${spent:.2f}; unlabelled {len(missing)}{failures}")
    if missing:
        raise SystemExit(1)


def share(counter, key, total):
    return f"{counter[key] / total * 100:.1f}%" if total else "-"


def headline(rows):
    graded = [r for r in rows if r["kind"] in ANSWERED and r["recommended"] and not r["multi"]]
    counts = Counter(r["kind"] for r in graded)
    n = len(graded)
    return n, counts, " / ".join(f"{k} {counts[k]} ({share(counts, k, n)})" for k in ANSWERED)


def cmd_stats(args):
    rows = read_jsonl(args.rows)
    labels = current_labels(rows, args.labels) if args.labels else {}
    print_coverage(args.rows)
    print(f"questionnaires {len({r['call'] for r in rows})}, questions {len(rows)}")
    print("kinds:", dict(Counter(r["kind"] for r in rows)))
    print("declined questionnaires:", len({r["call"] for r in rows if r["kind"] == "declined"}))
    print("hook-blocked questionnaires:", len({r["call"] for r in rows if r["kind"] == "hook_blocked"}),
          "(the owner never saw them; not a decline)")
    n, counts, line = headline(rows)
    print(f"single choice with a recommendation, answered: {n}: {line}")
    changed = [r for r in rows if r["kind"] == "changed"]
    positions = Counter(r["picked"][0] for r in changed)
    print("changed answers by option position (0 = first):", dict(sorted(positions.items())))
    for kind in ANSWERED:
        seconds = [r["seconds"] for r in rows if r["kind"] == kind and r["seconds"] is not None and r["recommended"] and not r["multi"]]
        if seconds:
            print(f"median answer time, {kind}: {statistics.median(seconds):.0f}s over {len(seconds)} single-question questionnaires")
    if args.split:
        for name, part in (("before", [r for r in rows if r["ts"] < args.split]), ("after", [r for r in rows if r["ts"] >= args.split])):
            print(f"{name} {args.split}: " + "{} graded: {}".format(*headline(part)[::2]))
    if not args.labels:
        return
    graded = [r for r in rows if r["id"] in labels]
    print(f"\nlabelled {len(graded)} of {n}")
    print("by recommended action: n accepted changed own")
    for action, members in sorted(_group(graded, labels, "rec_action").items(), key=lambda kv: -len(kv[1])):
        c = Counter(r["kind"] for r in members)
        print(f"  {action:15s} {len(members):4d} {share(c, 'accepted', len(members)):>7s} {share(c, 'changed', len(members)):>7s} {share(c, 'own', len(members)):>7s}")
        if args.split:
            for name, part in (("before", [r for r in members if r["ts"] < args.split]), ("after", [r for r in members if r["ts"] >= args.split])):
                pc = Counter(r["kind"] for r in part)
                print(f"      {name:6s} {len(part):4d} {share(pc, 'accepted', len(part)):>7s}")
    overridden = [r for r in graded if r["kind"] != "accepted"]
    print("\noverrides by theme:", dict(Counter(labels[r["id"]]["theme"] for r in overridden).most_common()))
    pairs = Counter((labels[r["id"]]["rec_action"], labels[r["id"]]["chosen_action"]) for r in overridden)
    print("overrides, recommended -> chosen (top 15):")
    for (rec, chosen), count in pairs.most_common(15):
        print(f"  {rec} -> {chosen}: {count}")


def _group(rows, labels, field):
    groups = {}
    for row in rows:
        groups.setdefault(labels[row["id"]][field], []).append(row)
    return groups


def mask(text):
    return SECRET.sub("[redacted]", text or "")


def date_format(tz):
    """Dates in the owner's time zone: the one `--tz` names, else this machine's.

    A zone this system cannot load (Windows without the tzdata package) prints UTC,
    marked Z, and says so once.
    """
    if not tz:
        return lambda ts: parse_ts(ts).astimezone().strftime("%m-%d %H:%M")
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception as error:
        print(f"time zone {tz} unavailable here ({error}); dates print in UTC, marked Z", file=sys.stderr)
        return lambda ts: parse_ts(ts).astimezone(timezone.utc).strftime("%m-%d %H:%MZ")
    return lambda ts: parse_ts(ts).astimezone(zone).strftime("%m-%d %H:%M")


def cmd_leads(args):
    rows = read_jsonl(args.rows)
    labels = current_labels(rows, args.labels) if args.labels else {}
    print_coverage(args.rows)
    local_date = date_format(args.tz)
    for row in rows:
        if row["kind"] not in ("changed", "own", "declined"):
            continue
        recommended = row["options"][row["recommended"][0]]["label"] if row["recommended"] else "(none marked)"
        if row["kind"] == "declined":
            answer = f"declined; then wrote: {row['after'][:400]}"
        elif row["kind"] == "changed":
            answer = row["options"][row["picked"][0]]["label"]
        else:
            answer = row["own_text"]
        theme = f" [{labels[row['id']]['theme']}]" if row["id"] in labels else ""
        print(mask(f"[{local_date(row['ts'])}]{theme} {row['header']} | Q: {row['question'][:160]} | recommended: {recommended[:90]} | {row['kind']}: {answer[:450]}"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--since", required=True, help="ISO UTC timestamp, e.g. 2026-01-01T00:00:00Z")
    ex.add_argument("--until", default="")
    ex.add_argument("--projects", action="append", help="repeat for explicit log roots; default: ~/.claude/projects")
    ex.add_argument("--out", required=True)
    ex.set_defaults(func=cmd_extract)
    cl = sub.add_parser("classify")
    cl.add_argument("--rows", required=True)
    cl.add_argument("--labels", required=True)
    cl.add_argument("--classifier", default="claude:claude-haiku-4-5")
    cl.add_argument("--batch", type=int, default=25)
    cl.add_argument("--parallel", type=int, default=4)
    cl.add_argument("--timeout", type=int, default=600)
    cl.set_defaults(func=cmd_classify)
    st = sub.add_parser("stats")
    st.add_argument("--rows", required=True)
    st.add_argument("--labels", default="")
    st.add_argument("--split", default="", help="ISO UTC timestamp to compare before and after")
    st.set_defaults(func=cmd_stats)
    le = sub.add_parser("leads")
    le.add_argument("--rows", required=True)
    le.add_argument("--labels", default="")
    le.add_argument("--tz", default="", help="IANA time zone for the dates, e.g. America/New_York; default: this machine's")
    le.set_defaults(func=cmd_leads)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
