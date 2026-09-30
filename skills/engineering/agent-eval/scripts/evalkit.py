#!/usr/bin/env -S uv run --locked --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Evaluate agent models and thinking efforts on Eval Cases taken from real owner corrections.

Each run gives one candidate (a model at one effort) one Eval Case: the owner's
original instruction, a neutral statement of the facts known then, and the
repository snapshot from just before the correction. The candidate answers and
writes a delivery plan, alone (`solo`), through plan -> fixed reviewer -> its own
revision (`review`), or by rewriting an earlier draft under forced adoption of
that review (`adopt`). Two judges score the two-level Trap and the rubric; cost
comes from the client's own usage records. Results are reference evidence only.

An implementation case (`"kind": "implement"`) asks the candidate to change the
snapshot instead of planning. It runs with write tools inside the same sandbox;
hidden tests taken from the real fix are placed only after the candidate stops,
and the judges score the Trap on the candidate's diff. Delivery is `direct`
(the implementer plans for itself), `split-<planner>` (a fixed planner writes
the plan once per case and repeat, and every implementer receives that same
plan) or `given-plan` (the case itself carries the approved plan).

Isolation is the precondition for every number. Codex candidates run inside a
sandbox (macOS `sandbox-exec` or Linux `bwrap`) that denies content reads outside
the run directory; Claude candidates run in `--restricted` mode with read-only
file tools confined to the snapshot. All repository and machine specifics come
from the config file (`--config`, see the skill's assets/config.example.json);
its `hosts` entries let one config serve several machines.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import concurrent.futures
import datetime as dt
import fcntl
import glob
import hashlib
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# Everything repository- or machine-specific comes from the config file (see
# assets/config.example.json); `configure` fills these module settings.
CFG: dict = {}
REPO: Path = Path(".")
CASES: Path = Path("cases")
RESULTS: Path = Path("agent-eval-results")
SCRATCH_ROOT = Path("/private/tmp/agent-eval")
CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")
CODEX_BIN = "codex"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
GATEWAY_URL = ""
GATEWAY_TOKEN_COMMAND: list = []
GATEWAY_KEY_ENV = "AGENT_EVAL_GATEWAY_KEY"
INSTRUCTIONS_FILE = "AGENTS.md"
# Files laid over every snapshot at their relative paths (config overlay_dir); None keeps snapshots as archived.
OVERLAY_DIR: Path | None = None
# The sandbox that confines candidates, by platform; config isolation.sandbox overrides.
PLATFORM_SANDBOX = {"darwin": "sandbox-exec", "linux": "bwrap"}
# User data, mounts, shared temp trees and (on Linux) local service sockets such as Docker's,
# whose contents a candidate must not reach; config isolation.deny_roots overrides. /opt,
# /usr/local and /etc stay readable: candidate CLIs install there.
DEFAULT_DENY_ROOTS = {
    "sandbox-exec": ("/Users", "/Volumes", "/private/tmp"),
    "bwrap": ("/home", "/root", "/mnt", "/media", "/nas", "/srv", "/tmp", "/var/tmp", "/run"),
}
SANDBOX = PLATFORM_SANDBOX.get(sys.platform, "")
DENY_ROOTS = DEFAULT_DENY_ROOTS.get(SANDBOX, ())
BWRAP = "/usr/bin/bwrap"
HOST: str | None = None
CLAUDE_TOOLS = "Read,Glob,Grep"
IMPL_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
# Paths a sandboxed candidate may read besides its run directory: the shared
# dependency-only test venv, its interpreter and the Claude CLI install.
ALLOW_READ: tuple = ()
TEST_PYTHON = ""
# Environment a candidate must never inherit: forge, vault, cloud and routing credentials.
FORBIDDEN_ENV_PREFIXES = ("GH_", "GITHUB_", "OP_", "AWS_", "SSH_AUTH_SOCK")

# Every string a candidate, the reviewer, a judge or the owner reads, by key. The config's
# `text` names a JSON file that replaces the whole table, for example in another language;
# `evalkit.py text` prints the table to start one from. Candidate, reviewer and judge entries
# are experiment inputs: each ledger row records their digest, and a batch refuses rows on
# another text (check_batch_text).
TEXT_EN = {
    "candidate.background": "Background:",
    "candidate.attachment": "Attachment {name}",
    "candidate.attachment_heading": "{label}:",
    "candidate.owner": "The owner (verbatim, in order):",
    "candidate.owner_message": "> {message}",
    "candidate.interfaces": "The acceptance tests call the interfaces below; keep their names and signatures, the behaviour is yours to decide:",
    "candidate.adopt": (
        "\n\n=== Previous round ===\nBelow are the answer and plan you returned for this task last round, and a reviewer's comments on them. "
        "Adopt every comment and return the complete revised answer and plan. You may decline a comment only by citing concrete "
        "repository evidence (file and line) that it is wrong, and you must write that evidence down.\n\n"
        "=== Your previous answer and plan ===\n{draft}\n\n=== Review ===\n{review}\n"),
    "candidate.revise": (
        "A reviewer commented on your answer and plan as below. Revise accordingly and return the complete "
        "revised answer and plan; explain any comment you disagree with.\n\n=== Review ===\n{review}"),
    "candidate.plan_heading": "=== Plan ===",
    "reviewer.prompt": (
        "You review this delivery plan. Below are the task given to a candidate agent and the answer and plan it returned. "
        "You may read the repository. List factual errors, missed risks, what should be verified first and what could be "
        "simpler, one point at a time, each with its evidence. Do not rewrite the plan for it.\n\n=== Task ===\n"
        "{task}\n\n=== Candidate's answer and plan ===\n{draft}\n"),
    "judge.prompt": (
        "You are an eval judge. Below are an eval case, an anonymous candidate agent's tool-call trajectory and its final answer. "
        "Judge only against the given pass conditions and rubric, item by item; do not grade style. Every verdict cites a "
        "trajectory step number or the answer's own words as evidence; no evidence means fail. Equivalent approaches are "
        "accepted as listed under 'Accepted equivalents'.\n\n"
        "=== Case as the candidate saw it ===\n{prompt}\n\n"
        "=== Trap (judged at two levels) ===\nThe original agent's mistake: {description}\n"
        "direction (right way; only the direction of the recommended approach, details need not be complete): {direction}\n"
        "pass (done right): {trap_pass}\n"
        "If pass is true, direction is true.\n\n"
        "=== Rubric (judge each) ===\n{items}\n\n=== Accepted equivalents ===\n{equivalents}\n\n"
        "=== Candidate trajectory ===\n{trajectory}\n\n=== Candidate final answer ===\n{answer}\n\n"
        "Output JSON: one trap entry (direction, pass, evidence) and one items entry per rubric id ({ids})."),
    "judge.no_steps": "(no tool calls recorded)",
    "judge.rubric_item": "- {id}: {text} (evidence: {evidence})",
    "judge.diff_heading": "=== Candidate's changes (git diff against the snapshot, {files} files, +{added}/-{deleted}) ===",
    "judge.diff_omitted": "\n(diff omitted for length: {files})",
    "judge.no_changes": "(no changes)",
    "report.md_title": "## Data: batch `{batch}`",
    "report.md_intro": (
        "{valid} valid runs, {invalid} invalid; run cost ${run_cost:.2f} (reviewer included; a shared plan counted once), "
        "judging cost ${judge_cost:.2f}. Trap is shown as direction/pass: ✓ both judges pass, ✗ both fail, ? they disagree or "
        "did not judge; rubric is the number of items both judges pass / total."),
    "report.md_overlay": "Snapshot code unchanged; these instruction files were replaced by the overlay_dir versions: {files}.",
    "report.md_hosts": "Valid runs come from more than one machine, whose CLIs may differ: {hosts}. Each row's host and sandbox are in runs.jsonl.",
    "report.md_host_item": "{host} ({sandbox}) {runs}",
    "report.list_sep": ", ",
    "report.md_hidden": "{expected} hidden tests; the bare snapshot passes {baseline}.",
    "report.impl_columns": ["Model", "effort", "Delivery", "Hidden tests (per run)", "Trap direction/pass", "Rubric",
                            "Mean cost", "Mean time"],
    "report.impl_html_columns": ["Model", "effort", "Delivery", "Hidden tests (per run)", "Mean pass rate",
                                 "Trap direction/pass", "Rubric", "Diff +/-", "Mean cost", "Mean time"],
    "report.plan_columns": ["Model", "effort", "Delivery", "Trap (draft)", "Rubric (draft)", "Trap (final)",
                            "Rubric (final)", "Chain cost", "Time"],
    "report.md_dispute_trap": "{run}: trap",
    "report.md_dispute_target_trap": "{run} {target}: trap",
    "report.same_as_draft": "= draft",
    "report.md_disputes": "### Judge disagreements",
    "report.md_invalid": "### Invalid runs",
    "report.md_invalid_item": "- `{run}`: {reasons}",
    "report.none": "none",
    "report.impl_lede": (
        "{expected} hidden tests; the bare snapshot passes {baseline} (the floor). Trap direction/pass and rubric count "
        "only when both judges pass. Cost includes the full shared plan (split modes)."),
    "report.html_dispute_trap": "{run}: trap",
    "report.html_dispute_target_trap": "{run} {target}: trap",
    "report.pass": "pass",
    "report.fail": "fail",
    "report.split_unjudged": "split/unjudged",
    "report.split_items": "split {ids}",
    "report.html_invalid_sep": ": ",
    "report.title": "Agent eval report {batch}",
    "report.heading": "Agent eval report · {batch}",
    "report.font": "system-ui,sans-serif",
    "report.lede": (
        "Reference results; nothing here changes your agent configuration. Trap and rubric count only when both judges pass;\n"
        "disagreements are listed for the owner to rule on. Cost is at list prices over the whole chain including reviewer and revision;\n"
        "judging is not included. {valid} valid runs, {invalid} invalid."),
    "report.disputes": "Disagreements for the owner to rule on",
    "report.invalid": "Invalid runs",
    "report.invalid_cost": "Invalid runs spent a further ${cost:.2f} at list prices, not included in the run cost above.",
    "report.dispute_item": "{head}: {item} · {text}",
    "report.dispute_verdict": "{judge}: {verdict} — {evidence}",
    "report.dispute_trap_verdict": "{judge}: direction {direction}, pass {passed} — {evidence}",
    "report.timed_out": "timed out {n}/{m}",
    "report.reference_only": "{n} reference-only hidden tests run but are not scored.",
    "report.ceiling": "Reachable ceiling {ceiling}/{scored}: the scored tests less those no candidate passed. Candidates passed {lo}–{hi}.",
    "report.never_passed": "Tests the reference passes that no candidate passed ({n}): {tests}",
    "report.no_discrimination": "No discrimination: the candidates' range ({spread}) is below implement.min_spread ({min_spread}); this case does not rank them.",
    "report.baseline_heading": "Baseline cases",
    "report.baseline_note": "These cases check whether an implementer can finish an approved plan; they are not used to compare models.",
    "page.font": '"IBM Plex Sans",system-ui,sans-serif',
    "page.copied": "Copied — paste it back into the chat",
    "page.copy_by_hand": "Copy the box below by hand",
    "picker.title": "Pick eval cases",
    "picker.lede": (
        "Each candidate is a moment in a real session where an agent went wrong and the owner corrected it. Tick the ones "
        "to turn into eval cases, ideally of different error types, then press \"Copy selection\" at the bottom and paste it "
        "back into the chat."),
    "picker.copy": "Copy selection",
    "picker.selection": "Selection",
    "picker.recommended": "recommended",
    "picker.task": "Original task",
    "picker.wrong": "What went wrong",
    "picker.fix": "Owner correction",
    "picker.quote_open": "“",
    "picker.quote_close": "”",
    "picker.right": "Right direction",
    "picker.snapshot": "snapshot",
    "picker.selected": "{n} selected",
    "picker.tag_sep": "; ",
    "picker.picked": "Picked: ",
    "picker.title_open": " (",
    "picker.title_close": ")",
    "picker.list_sep": ", ",
    "casepage.title": "Review eval cases",
    "casepage.lede": (
        "One tab per case: the prompt the candidate sees, the two-level Trap and each rubric item. Mark every item agree / "
        "change / drop, add a note to anything you change, then press \"Copy marks\" at the bottom and paste it back into "
        "the chat. Marks are stored only in this browser."),
    "casepage.copy": "Copy marks",
    "casepage.marks": "Marks",
    "casepage.evidence": "Evidence: ",
    "casepage.agree": "agree",
    "casepage.change": "change",
    "casepage.drop": "drop",
    "casepage.note": "Note (optional)",
    "casepage.placed": "placed in snapshot at",
    "casepage.inline": " (inline in the prompt)",
    "casepage.item_sep": "; ",
    "casepage.none": "none",
    "casepage.snapshot": "Snapshot SHA",
    "casepage.attachments": "Attachments",
    "casepage.prompt_heading": "Prompt (everything the candidate sees)",
    "casepage.prompt": "Prompt",
    "casepage.prompt_check": "Is the prompt neutral, with no hint of the answer, and are any background facts missing?",
    "casepage.impl_heading": "Implementation case: hidden tests and delivery modes",
    "casepage.modes": "Delivery modes:",
    "casepage.label_gap": " ",
    "casepage.list_sep": ", ",
    "casepage.hidden": "Hidden tests",
    "casepage.hidden_where": " (invisible to the candidate, placed in the worktree after it finishes): ",
    "casepage.hidden_count_before": ", ",
    "casepage.hidden_count_after": " in total. ",
    "casepage.tests_check": "Score implementations with these tests (passed / total)",
    "casepage.trap_heading": "Trap (two levels, each pass/fail)",
    "casepage.mistake": "The original mistake:",
    "casepage.direction": "Direction",
    "casepage.pass": "Pass",
    "casepage.rubric_heading": "Rubric (each judged yes or no)",
    "casepage.equivalents": "Accepted equivalents:",
    "casepage.ask": "Your decision:",
    "casepage.answer": "Answer",
    "casepage.answer_hint": "agree = judge as drafted; otherwise write your ruling in the note",
    "casepage.leak_heading": "Leak markers (must not appear in the snapshot or the prompt)",
    "casepage.shared": "Shared rubric",
    "casepage.count": "{done} / {total} marked · {edit} to change or drop",
    "casepage.marks_heading": "Eval case review marks",
    "casepage.unmarked": "unmarked",
    "casepage.lint_heading": "Case checks (evalkit.py lint-case)",
    "casepage.lint_failed": "The checks could not run: ",
    "casepage.checked_by": "Checked by: ",
    "casepage.unchecked": "No test or rubric item named as its check",
    "casepage.kinds": "applies to: ",
    "casepage.role_baseline": "Baseline case: it checks that an implementer can finish an approved plan and is not used to compare models.",
    "casepage.reference_only": "Reference-only tests (they run but are not scored): ",
    "casepage.alt_patches": "Alternative implementations the hidden tests must accept:",
    "casepage.accepted_failures": "Accepted failures: ",
    "casepage.wrong_markers": "Wrong-approach markers (the lint warns where the case material matches one):",
    "lint.error": "ERROR",
    "lint.warning": "WARNING",
    "lint.literal": "a hidden test asserts wording the candidate never reads",
    "lint.name": "a hidden test uses a name that is neither in the snapshot nor in what the candidate reads",
    "lint.syntax": "a hidden test file does not parse",
    "lint.checked_by": "a trap.pass clause cites a rubric item or hidden test that does not exist",
    "lint.unmapped": "a trap.pass clause names no rubric item or hidden test that checks it",
    "lint.pass_string": "trap.pass is one string: write it as clauses, each mapped to a test or rubric item",
    "lint.direction": "trap.direction contains a phrase from lint.direction_forbidden",
    "lint.wrong_marker": "the case material itself suggests the wrong approach (trap.wrong_markers)",
    "lint.bad_regex": "a trap.wrong_markers entry is not a valid regular expression",
    "lint.more": "(+{n} more)",
    "lint.docstrings": "Hidden tests touching an undisclosed name or wording, with what each says it checks; compare them with the plan:",
    "lint.clean": "no findings",
}
EXPERIMENT_TEXT = ("candidate.", "reviewer.", "judge.")


def text_digest(text):
    """What candidates, the reviewer and the judges read, as one short hash."""
    inputs = {k: v for k, v in text.items() if k.startswith(EXPERIMENT_TEXT)}
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


T = dict(TEXT_EN)
TEXT_DIGEST = text_digest(T)
KIT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def fill_text(page):
    """An owner page template with every %%key%% replaced by its text entry."""
    return re.sub(r"%%([a-z]+\.[a-z_]+)%%", lambda m: T[m.group(1)], page)


def host_config(cfg, host=None):
    """The config with this machine's `hosts` entry merged over the top level, and that entry's name.

    One repository's config serves several machines, whose paths, binaries and sandbox differ.
    The entry is the one named by `host`, else the one matching this machine's short hostname
    (case-insensitive); a named host that is not declared is an error. A nested dict merges one
    level deep, so a host can replace isolation.deny_roots alone. A host may not replace the
    text: the text digest would then differ by machine.
    """
    hosts = cfg.get("hosts") or {}
    names = {k.lower(): k for k in hosts}
    wanted = (host or socket.gethostname().split(".")[0]).lower()
    if host is not None and wanted not in names:
        raise EvalError(f"--host {host} is not declared under hosts (declared: {sorted(hosts)})")
    merged = {k: v for k, v in cfg.items() if k != "hosts"}
    name = names.get(wanted)
    if name is None:
        return merged, None
    if "text" in hosts[name]:
        raise EvalError(f"hosts.{name} sets text; the text must be the same on every machine")
    for key, value in hosts[name].items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            value = {**merged[key], **value}
        merged[key] = value
    return merged, name


def configure(path, host=None):
    """Load the eval config for this machine; relative paths resolve against the config file's directory."""
    global CFG, REPO, CASES, RESULTS, SCRATCH_ROOT, CLAUDE_BIN, CODEX_BIN, GATEWAY_URL, \
        GATEWAY_TOKEN_COMMAND, GATEWAY_KEY_ENV, INSTRUCTIONS_FILE, SANDBOX, DENY_ROOTS, FORBIDDEN_ENV_PREFIXES, \
        ALLOW_READ, TEST_PYTHON, OVERLAY_DIR, HOST, T, TEXT_DIGEST
    path = Path(path).expanduser().resolve()
    CFG, HOST = host_config(json.loads(path.read_text(encoding="utf-8")), host)
    base = path.parent

    def rel(value):
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else (base / p).resolve()

    REPO = rel(CFG.get("repo", "."))
    CASES = rel(CFG.get("cases_dir", "cases"))
    RESULTS = rel(CFG.get("results_dir", "results"))
    SCRATCH_ROOT = Path(os.path.expanduser(CFG.get("scratch_root", "/private/tmp/agent-eval")))
    CLAUDE_BIN = os.path.expanduser(CFG.get("claude_bin", CLAUDE_BIN))
    CODEX_BIN = os.path.expanduser(CFG.get("codex_bin", CODEX_BIN))
    gateway = CFG.get("gateway") or {}
    GATEWAY_URL = gateway.get("url", "")
    GATEWAY_TOKEN_COMMAND = [os.path.expanduser(a) for a in gateway.get("token_command", [])]
    GATEWAY_KEY_ENV = gateway.get("key_env", GATEWAY_KEY_ENV)
    INSTRUCTIONS_FILE = CFG.get("instructions_file", INSTRUCTIONS_FILE)
    OVERLAY_DIR = rel(CFG["overlay_dir"]) if CFG.get("overlay_dir") else None
    iso = CFG.get("isolation") or {}
    SANDBOX = iso.get("sandbox") or PLATFORM_SANDBOX.get(sys.platform, "")
    if SANDBOX not in DEFAULT_DENY_ROOTS:
        raise EvalError(f"no sandbox {SANDBOX!r} on platform {sys.platform}: set isolation.sandbox to one of "
                        f"{sorted(DEFAULT_DENY_ROOTS)}; candidates never run unsandboxed")
    DENY_ROOTS = tuple(iso.get("deny_roots", DEFAULT_DENY_ROOTS[SANDBOX]))
    FORBIDDEN_ENV_PREFIXES = tuple(iso.get("forbidden_env_prefixes", FORBIDDEN_ENV_PREFIXES))
    # Both the configured path and its resolved target: a symlinked CLI is checked at each.
    ALLOW_READ = tuple(dict.fromkeys(q for p in iso.get("allow_read", [])
                                     for q in (os.path.expanduser(p), os.path.realpath(os.path.expanduser(p)))))
    TEST_PYTHON = os.path.expanduser((CFG.get("implement") or {}).get("test_python", ""))
    T = dict(TEXT_EN)
    if CFG.get("text"):
        text_path = rel(CFG["text"])
        loaded = json.loads(text_path.read_text(encoding="utf-8"))
        missing, unknown = sorted(set(TEXT_EN) - set(loaded)), sorted(set(loaded) - set(TEXT_EN))
        if missing or unknown:
            raise EvalError(f"text {text_path}: missing {missing}, unknown {unknown}; `evalkit.py text` prints every entry")
        T = loaded
    TEXT_DIGEST = text_digest(T)
    for key in ("candidates", "modes", "reviewer", "judges", "prices"):
        if key not in CFG:
            raise EvalError(f"config {path} lacks '{key}'")


GATEWAY_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY")
DEFAULT_TIMEOUT = 3600
TAIL = 4000


class EvalError(Exception):
    pass


# ---------------------------------------------------------------- definitions

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_cases(ids=None):
    common = load_json(CASES / "common.json")
    cases = {}
    for path in sorted(CASES.glob("*/case.json")):
        case = load_json(path)
        case["dir"] = path.parent
        cases[case["id"]] = case
    if ids:
        missing = set(ids) - set(cases)
        if missing:
            raise EvalError(f"unknown case ids: {sorted(missing)}")
        cases = {k: v for k, v in cases.items() if k in ids}
    return common, cases


def case_digest(case):
    """Every file of the case directory and the shared common.json, as one short hash.

    Calibration records are keyed by it, so an edit to any case file, a hidden test or the
    shared rubric makes the case uncalibrated again. Dotfiles and __pycache__ are skipped.
    """
    h = hashlib.sha256()
    root = Path(case["dir"])
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        h.update(rel.as_posix().encode("utf-8") + b"\0" + p.read_bytes() + b"\0")
    h.update((CASES / "common.json").read_bytes())
    return h.hexdigest()[:12]


def judges_digest():
    """The configured judges (id, runtime, model, effort, client), as one short hash."""
    fields = ("id", "runtime", "model", "effort", "client")
    judges = [{f: j.get(f) for f in fields} for j in CFG.get("judges", [])]
    return hashlib.sha256(json.dumps(judges, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def calibration_key(kind, digest):
    """A judges record holds for one case state, one set of judges and one text; a tests record for the case alone."""
    if kind != "judges":
        return digest
    return hashlib.sha256(f"{digest}|{judges_digest()}|{TEXT_DIGEST}".encode("utf-8")).hexdigest()[:12]


def calibration_path(kind, case_id, digest):
    return RESULTS / "calibrations" / f"{kind}-{case_id}-{calibration_key(kind, digest)}.json"


def write_calibration(kind, case_id, digest, body):
    """One record per case state: `judges` from `calibrate`, `tests` from `calibrate-tests`."""
    path = calibration_path(kind, case_id, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = {"judges_digest": judges_digest(), "calibration_key": calibration_key(kind, digest)} \
        if kind == "judges" else {}
    row = {"kind": kind, "case": case_id, "case_digest": digest, **extra, **body, "text_digest": TEXT_DIGEST,
           "kit_digest": KIT_DIGEST, "host": HOST, "at": now()}
    path.write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def read_calibration(kind, case_id, digest):
    path = calibration_path(kind, case_id, digest)
    return load_json(path) if path.exists() else None


def calibration_gaps(cases):
    """What `run` refuses to launch on: each case needs a passing calibration of its current digest.

    Judges must fail the negative and pass the positive, under the configured judges and text
    (changing either needs a new `calibrate`); an implementation case's hidden tests must also
    have passed `calibrate-tests`. Sets `case["digest"]` for the ledger rows.
    """
    gaps = []
    for case_id, case in cases.items():
        digest = case["digest"] = case_digest(case)
        needed = [("judges", f"calibrate --batch <batch> --cases {case_id}")]
        if is_impl(case):
            needed.append(("tests", f"calibrate-tests --case {case_id}"))
        for kind, command in needed:
            rec = read_calibration(kind, case_id, digest)
            if not rec or not rec.get("ok"):
                state = "failed" if rec else "missing"
                scope = (f"digest {digest}, judges {judges_digest()}, text {TEXT_DIGEST}" if kind == "judges"
                         else f"digest {digest}")
                gaps.append(f"case {case_id} ({scope}): {kind} calibration {state}; run `evalkit.py {command}`")
    return gaps


def check_calibrated(cases, dry_run=False):
    """No run starts on a case whose Trap is not shown passable and failable as written."""
    gaps = calibration_gaps(cases)
    if gaps and not dry_run:
        raise EvalError("uncalibrated cases, nothing launched: " + " | ".join(gaps))
    for gap in gaps:
        print(f"not calibrated (a real run refuses): {gap}")


def load_arms():
    return CFG


def load_prices():
    return CFG["prices"]


def expand_runs(cases, arms, modes=None, candidates=None, efforts=None, repeats=None):
    """Planning runs; with `repeats` each arm runs that many times as <id>-rN, without it once under <id>."""
    runs = []
    for case_id, case in cases.items():
        if case.get("kind") == "implement":
            continue
        for cand in arms["candidates"]:
            if candidates and cand["id"] not in candidates:
                continue
            for effort in cand["efforts"]:
                if efforts and effort not in efforts:
                    continue
                for mode in arms["modes"]:
                    if modes and mode not in modes:
                        continue
                    for rep in range(1, (repeats or 1) + 1):
                        suffix = f"-r{rep}" if repeats else ""
                        runs.append({"run_id": f"{case_id}-{cand['id']}-{effort}-{mode}{suffix}", "case": case_id,
                                     "candidate": cand, "effort": effort, "mode": mode,
                                     "case_digest": case.get("digest")})
    return runs


def build_prompt(case, common, suffix=None):
    lines = [T["candidate.background"]]
    lines += [f"{i}. {item}" for i, item in enumerate(case["background"], 1)]
    for att in case.get("attachments", []):
        if att.get("inline"):
            text = (case["dir"] / att["file"]).read_text(encoding="utf-8")
            label = att.get("label") or T["candidate.attachment"].format(name=Path(att["file"]).name)
            lines += ["", T["candidate.attachment_heading"].format(label=label), text.strip()]
    lines += ["", T["candidate.owner"]]
    lines += [T["candidate.owner_message"].format(message=m) for m in case["owner_messages"]]
    if case.get("interface"):
        lines += ["", T["candidate.interfaces"]]
        lines += [f"- {item}" for item in case["interface"]]
    if suffix is None:
        suffix = common["implement_suffix"] if case.get("kind") == "implement" else common["suffix"]
    lines += ["", suffix]
    return "\n".join(lines)


# ------------------------------------------------------------------ snapshot

def run_dir(batch, run_id):
    return SCRATCH_ROOT / batch / run_id


def overlay_files():
    """{relative path: sha256 prefix} of the files laid over every snapshot; empty without overlay_dir.

    An overlay measures an instruction change on unchanged cases: the snapshot's code stays as
    archived and only these files are replaced or added. Leak markers still apply to them.
    """
    if OVERLAY_DIR is None:
        return {}
    if not OVERLAY_DIR.is_dir():
        raise EvalError(f"overlay_dir {OVERLAY_DIR} is not a directory")
    return {str(p.relative_to(OVERLAY_DIR)): hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            for p in sorted(OVERLAY_DIR.rglob("*")) if p.is_file()}


def prepare_snapshot(case, root):
    """Extract the case SHA into `root/wt` as a one-commit repository with no later history.

    Overlay files go in before that commit, so an implementer's diff never contains them.
    """
    wt = root / "wt"
    if root.exists():
        shutil.rmtree(root)
    for sub in ("wt", "home", "tmp"):
        (root / sub).mkdir(parents=True)
    archive = subprocess.Popen(["git", "-C", str(REPO), "archive", case["snapshot"]], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(wt)], stdin=archive.stdout, check=True)
    if archive.wait() != 0:
        raise EvalError(f"git archive {case['snapshot']} failed")
    for rel_path in overlay_files():
        target = wt / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OVERLAY_DIR / rel_path, target)
    git = ["git", "-C", str(wt), "-c", "user.name=agent-eval", "-c", "user.email=agent-eval@local"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([*git, "commit", "-qm", f"snapshot {case['snapshot'][:9]}"], check=True)
    for att in case.get("attachments", []):
        if att.get("place"):
            target = wt / att["place"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(case["dir"] / att["file"], target)
    return wt


def write_codex_home(home, client="gateway"):
    home.mkdir(parents=True, exist_ok=True)
    if client == "native":
        # Codex's own login; the file sits inside the run directory the candidate can read.
        auth = Path.home() / ".codex" / "auth.json"
        if auth.exists():
            shutil.copyfile(auth, home / "auth.json")
        (home / "config.toml").write_text("", encoding="utf-8")
        return
    (home / "config.toml").write_text(
        'model_provider = "gateway"\n'
        "[model_providers.gateway]\n"
        'name = "OpenAI-compatible gateway"\n'
        f'base_url = "{GATEWAY_URL}/v1"\n'
        'wire_api = "responses"\n'
        f'env_key = "{GATEWAY_KEY_ENV}"\n'
        "request_max_retries = 2\n"
        "stream_max_retries = 2\n"
        "stream_idle_timeout_ms = 300000\n",
        encoding="utf-8",
    )


# The Mach services a sandboxed Codex cannot start without (0.159, measured 2026-09-30 by
# removing one service at a time): preferences (it fails "Failed to synchronize managed
# preferences" without them) and certificate trust. Preference reads through them are denied
# per domain; the login keychain stays unreadable because its file is under a deny root.
SBPL_MACH_SERVICES = ("com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent",
                      "com.apple.trustd.agent", "com.apple.SecurityServer")


def sandbox_profile(root):
    """Deny reading contents of, and writing to, user data, mounts and every other temp tree,
    and every local service: unix sockets, Mach services and app launches.

    Metadata stays readable: Codex canonicalizes CODEX_HOME at start, which stats each
    parent under /private/tmp, and denying that aborts it before the first request
    (measured 2026-09-23). Listing a directory or reading a file is content and stays denied.
    Under `allow default` alone a candidate could connect to any unix socket (Docker's
    among them), read the pasteboard and every app's preferences, and `open` an app, which
    then runs outside the sandbox (measured 2026-09-30). Only the resolver's socket stays
    open, since the network is shared by design, and only the Mach services in
    SBPL_MACH_SERVICES.
    """
    root = os.path.realpath(root)
    extra = "".join(f'(allow file-read-data ({"subpath" if os.path.isdir(p) else "literal"} "{p}"))\n'
                    for p in ALLOW_READ)
    return (
        "(version 1)\n(allow default)\n"
        '(deny file-read-data file-write* ' + " ".join(f'(subpath "{d}")' for d in DENY_ROOTS) + ')\n'
        f'(allow file-read-data file-write* (subpath "{root}"))\n' + extra +
        f'(deny file-write* (require-not (require-any (subpath "{root}") '
        '(subpath "/private/var/folders") (subpath "/dev"))))\n'
        "(deny network-outbound (remote unix-socket))\n"
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))\n'
        "(deny mach-lookup)\n(allow mach-lookup " + " ".join(f'(global-name "{n}")' for n in SBPL_MACH_SERVICES) + ")\n"
        "(deny user-preference-read user-preference-write)\n(deny appleevent-send)\n(deny lsopen)\n"
    )


def under(path, roots):
    """Whether `path` is one of `roots` or inside one."""
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def bwrap_prefix(root):
    """The SBPL profile's guarantees as bubblewrap mounts.

    The host tree is read-only. Each deny root is replaced by an empty tmpfs (a denied file,
    such as a service socket, by /dev/null), so its contents are neither readable nor
    reachable, and a write there lands in memory that vanishes with the sandbox. Allowed
    reads are bound back read-only over those, and the run root last, read-write, so
    neither can be shadowed. Unlike SBPL, a denied tree's metadata is hidden too: bwrap
    creates only the parents of what it binds, which is all Codex needs to canonicalize
    CODEX_HOME. The resolver file is bound back when /run is denied, since the network is
    shared by design.
    """
    root = os.path.realpath(root)
    argv = [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    denied = []
    for d in sorted({os.path.realpath(d) for d in DENY_ROOTS}, key=lambda p: (len(p), p)):
        if not os.path.lexists(d) or under(d, denied):
            continue
        argv += ["--tmpfs", d] if os.path.isdir(d) else ["--ro-bind", "/dev/null", d]
        denied.append(d)
    resolver = os.path.dirname(os.path.realpath("/etc/resolv.conf"))
    bound = []
    for p in sorted({*ALLOW_READ, resolver}, key=lambda p: (len(p), p)):
        if not os.path.lexists(p) or not under(p, denied) or under(p, bound):
            continue
        argv += ["--symlink", os.readlink(p), p] if os.path.islink(p) else ["--ro-bind", p, p]
        bound.append(p)
    return argv + ["--bind", root, root, "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--"]


def sandbox_prefix(root):
    """The argv prefix that confines a command to the run root, for the configured sandbox."""
    if SANDBOX == "sandbox-exec":
        return ["/usr/bin/sandbox-exec", "-f", str(Path(root) / "sandbox.sb")]
    if SANDBOX == "bwrap":
        return bwrap_prefix(root)
    raise EvalError(f"no sandbox configured (isolation.sandbox is {SANDBOX!r})")


def sandboxed(root, argv):
    """`argv` run inside the run root's sandbox; the one place a confined command is built."""
    return [*sandbox_prefix(root), *argv]


def prepare_sandbox(root):
    """Write what the sandbox reads at launch: sandbox-exec reads a profile file; bwrap's policy is its argv."""
    if SANDBOX == "sandbox-exec":
        (Path(root) / "sandbox.sb").write_text(sandbox_profile(root), encoding="utf-8")


# ----------------------------------------------------------------- isolation

def leak_hits(paths_to_scan, markers):
    """Return files (and the prompt, as '<prompt>') that contain any leak marker."""
    hits = []
    markers = [m for m in markers if m]
    if not markers:
        return hits
    for base in paths_to_scan:
        if not Path(base).exists():
            continue
        args = ["grep", "-rIlF", "--exclude-dir=.git"]
        for m in markers:
            args += ["-e", m]
        proc = subprocess.run([*args, str(base)], capture_output=True, text=True)
        hits += [line for line in proc.stdout.splitlines() if line]
    return hits


def forbidden_env(env, runtime, client):
    bad = []
    for key in env:
        if key.startswith(FORBIDDEN_ENV_PREFIXES):
            bad.append(key)
        if key == "ANTHROPIC_API_KEY":
            bad.append(key)
    if runtime == "claude" and client == "native":
        bad += [k for k in GATEWAY_ENV if k in env]
    return sorted(set(bad))


def probe_sandbox(root, denied_file):
    """The sandbox must refuse a file outside the run root and allow one inside it."""
    inside = Path(root) / "wt" / INSTRUCTIONS_FILE
    failures = []
    outside = subprocess.run(sandboxed(root, ["/bin/cat", str(denied_file)]), capture_output=True)
    if outside.returncode == 0:
        failures.append(f"sandbox allowed reading {denied_file}")
    if inside.exists():
        ok = subprocess.run(sandboxed(root, ["/bin/cat", str(inside)]), capture_output=True)
        if ok.returncode != 0:
            failures.append(f"sandbox refused the snapshot's own {inside}")
    return failures


def claude_argv_ok(argv):
    failures = []
    if "--restricted" not in argv:
        failures.append("claude argv lacks --restricted")
    if "--strict-mcp-config" not in argv:
        failures.append("claude argv lacks --strict-mcp-config")
    if "--tools" not in argv or argv[argv.index("--tools") + 1] != CLAUDE_TOOLS:
        failures.append(f"claude tools are not exactly {CLAUDE_TOOLS}")
    return failures


def probe_test_python(root):
    """The shared test interpreter must start inside the sandbox, or no hidden test can run."""
    if not TEST_PYTHON:
        return ["implement: config has no implement.test_python"]
    proc = subprocess.run(sandboxed(root, [TEST_PYTHON, "-c", "import pytest"]),
                          capture_output=True, text=True, cwd=root,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(Path(root) / "home"), "TMPDIR": str(Path(root) / "tmp")})
    return [] if proc.returncode == 0 else [f"test python cannot start in the sandbox: {proc.stderr[-300:]}"]


def isolation_check(root, markers, prompt, env, runtime, client, argv=None, implement=False):
    """Every failure is a reason not to launch. An empty list is the only pass.

    A Codex or write-capable run is probed in the run root's sandbox (prepare_sandbox first)
    and its argv must start with exactly the prefix `sandboxed` builds for that root.
    """
    failures = []
    bad_env = forbidden_env(env, runtime, client)
    if bad_env:
        failures.append(f"forbidden environment: {bad_env}")
    hits = leak_hits([root], markers)
    if any(m in prompt for m in markers if m):
        hits.append("<prompt>")
    if hits:
        failures.append(f"leak markers found in: {hits[:5]}")
    if runtime == "codex" or implement:
        # Write-capable Claude runs inside the same sandbox as Codex (see claude_impl_argv).
        prefix = sandbox_prefix(root)
        if argv is None or argv[:len(prefix)] != prefix:
            failures.append(f"{runtime} argv is not wrapped in this run's {SANDBOX} sandbox")
        failures += probe_sandbox(root, REPO / INSTRUCTIONS_FILE)
        if implement:
            failures += probe_test_python(root)
        if implement and runtime == "claude" and argv is not None:
            if "--tools" not in argv or argv[argv.index("--tools") + 1] != IMPL_TOOLS:
                failures.append(f"claude tools are not exactly {IMPL_TOOLS}")
            if "--strict-mcp-config" not in argv:
                failures.append("claude argv lacks --strict-mcp-config")
    elif argv is not None:
        failures += claude_argv_ok(argv)
    return failures


# ------------------------------------------------------------------- launch

def gateway_key():
    if not GATEWAY_TOKEN_COMMAND:
        return ""  # no gateway configured: every model must be native
    key = subprocess.run(GATEWAY_TOKEN_COMMAND, capture_output=True, text=True, timeout=30).stdout.strip()
    if not key:
        raise EvalError("could not fetch the gateway key")
    return key


def claude_env(client, key):
    env = {k: v for k, v in os.environ.items() if not k.startswith(FORBIDDEN_ENV_PREFIXES)}
    env.pop("ANTHROPIC_API_KEY", None)
    for k in GATEWAY_ENV:
        env.pop(k, None)
    if client == "gateway":
        env["ANTHROPIC_BASE_URL"] = GATEWAY_URL
        env["ANTHROPIC_AUTH_TOKEN"] = key
    return env


def codex_env(root, codex_home, key):
    return {
        "PATH": os.pathsep.join(dict.fromkeys([os.path.dirname(shutil.which(CODEX_BIN) or CODEX_BIN), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])),
        "HOME": str(Path(root) / "home"),
        "TMPDIR": str(Path(root) / "tmp"),
        "CODEX_HOME": str(codex_home),
        "LANG": "en_US.UTF-8",
        GATEWAY_KEY_ENV: key,
    }


def instructions(wt):
    """The snapshot's own agent instructions, given explicitly because --restricted skips discovery."""
    f = Path(wt) / INSTRUCTIONS_FILE
    return f if f.exists() else None


def claude_argv(model, effort, append_file=None, resume=None, tools=CLAUDE_TOOLS, schema=None):
    argv = [str(CLAUDE_BIN), "-p", "--model", model, "--effort", effort, "--restricted",
            "--tools", tools, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if schema is not None:
        argv += ["--output-format", "json", "--json-schema", json.dumps(schema), "--no-session-persistence"]
    else:
        argv += ["--output-format", "stream-json", "--verbose"]
    if append_file:
        argv += ["--append-system-prompt-file", str(append_file)]
    if resume:
        argv += ["--resume", resume]
    return argv


def codex_argv(model, effort, root, cwd, resume=None, schema_path=None, out_path=None,
               sandbox="danger-full-access"):
    """Codex inside the sandbox of run root `root`; its own sandbox mode is set by `sandbox`."""
    argv = [CODEX_BIN, "exec"]
    if resume:
        argv += ["resume", resume]
    argv += ["--model", model, "-c", f"model_reasoning_effort={json.dumps(effort)}",
             "--skip-git-repo-check", "--json"]
    if resume:
        argv += ["-c", f"sandbox_mode={json.dumps(sandbox)}"]
    else:
        argv += ["--sandbox", sandbox, "-C", str(cwd)]
    if schema_path:
        argv += ["--output-schema", str(schema_path), "-o", str(out_path)]
    return sandboxed(root, argv + ["-"])


def run_process(argv, prompt, cwd, env, timeout, stdout_path, stderr_path):
    started = time.monotonic()
    with open(stdout_path, "w") as out, open(stderr_path, "w") as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True,
                                cwd=cwd, env=env, start_new_session=True)
        try:
            proc.communicate(prompt, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    return {"returncode": proc.returncode, "elapsed_s": round(time.monotonic() - started, 1),
            "timed_out": timed_out}


# -------------------------------------------------------------- usage & cost

def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def claude_transcript_usage(rows):
    """Attribute usage per model from a Claude transcript; one message may appear on several lines."""
    per_message = {}
    efforts = set()
    for row in rows:
        if isinstance(row.get("effort"), str):
            efforts.add(row["effort"])
        msg = row.get("message") or {}
        if msg.get("role") != "assistant" or not msg.get("id"):
            continue
        usage = msg.get("usage") or {}
        cache = usage.get("cache_creation") or {}
        entry = {
            "model": msg.get("model"),
            "input": usage.get("input_tokens", 0) or 0,
            "output": usage.get("output_tokens", 0) or 0,
            "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_write_5m": cache.get("ephemeral_5m_input_tokens", 0) or 0,
            "cache_write_1h": cache.get("ephemeral_1h_input_tokens", 0) or 0,
        }
        if not cache and usage.get("cache_creation_input_tokens"):
            entry["cache_write_5m"] = usage["cache_creation_input_tokens"]
        prev = per_message.get(msg["id"])
        per_message[msg["id"]] = entry if prev is None else {
            k: (max(prev[k], entry[k]) if k != "model" else (prev[k] or entry[k])) for k in entry}
    by_model = {}
    for entry in per_message.values():
        model = entry["model"] or "unknown"
        acc = by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0,
                                          "cache_write_5m": 0, "cache_write_1h": 0, "messages": 0})
        for k in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"):
            acc[k] += entry[k]
        acc["messages"] += 1
    return by_model, sorted(efforts)


def codex_rollout_usage(rows):
    """Codex records the session's cumulative usage; the last token_count holds the total."""
    total = None
    models, efforts = set(), set()
    for row in rows:
        payload = row.get("payload") or {}
        if row.get("type") == "turn_context":
            if payload.get("model"):
                models.add(payload["model"])
            if payload.get("effort"):
                efforts.add(payload["effort"])
        if row.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            if info.get("total_token_usage"):
                total = info["total_token_usage"]
    if total is None:
        return {}, sorted(models), sorted(efforts)
    model = sorted(models)[0] if len(models) == 1 else "+".join(sorted(models)) or "unknown"
    cached = total.get("cached_input_tokens", 0) or 0
    written = total.get("cache_write_input_tokens", 0) or 0
    usage = {model: {"input": max((total.get("input_tokens", 0) or 0) - cached - written, 0),
                     "cache_read": cached, "cache_write": written,
                     "output": total.get("output_tokens", 0) or 0,
                     "reasoning": total.get("reasoning_output_tokens", 0) or 0}}
    return usage, sorted(models), sorted(efforts)


def codex_usage_from_files(files):
    """Sum usage over every rollout of a Codex session, main thread and subagents alike.

    A candidate may spawn subagents; each gets its own rollout with its own cumulative
    total, and each request is billed. The model and effort a run is judged by come from
    the main thread only: a subagent's effort is the candidate's own choice (measured
    2026-09-23: an astra ultra run spawned an xhigh subagent).
    """
    usage, models, efforts, subagents = {}, set(), set(), []
    for path in sorted(files):
        rows = read_jsonl(path)
        meta = next((r.get("payload") or {} for r in rows if r.get("type") == "session_meta"), {})
        part, part_models, part_efforts = codex_rollout_usage(rows)
        usage = merge_usage(usage, part)
        if meta.get("thread_source") == "subagent" or meta.get("parent_thread_id"):
            subagents.append({"id": meta.get("id"), "role": meta.get("agent_role"),
                              "models": part_models, "efforts": part_efforts})
        else:
            models.update(part_models)
            efforts.update(part_efforts)
    return usage, sorted(models), sorted(efforts), subagents


def price_key(model, prices):
    base = re.sub(r"\[.*\]$", "", model or "")
    return base if base in prices else None


def cost_usd(usage_by_model, prices):
    """Price each model's tokens at list rates; an unpriced model makes the total unknown (None)."""
    total = 0.0
    for model, u in usage_by_model.items():
        key = price_key(model, prices)
        if key is None:
            return None
        p = prices[key]
        total += u.get("input", 0) * p["input"]
        total += u.get("output", 0) * p["output"]
        total += u.get("cache_read", 0) * p.get("cache_read", p["input"])
        total += u.get("cache_write", 0) * p.get("cache_write", p["input"])
        total += u.get("cache_write_5m", 0) * p.get("cache_write_5m", p["input"])
        total += u.get("cache_write_1h", 0) * p.get("cache_write_1h", p["input"])
    return round(total / 1_000_000, 6)


def merge_usage(*parts):
    out = {}
    for part in parts:
        for model, u in part.items():
            acc = out.setdefault(model, {})
            for k, v in u.items():
                acc[k] = acc.get(k, 0) + v
    return out


# --------------------------------------------------------------- trajectory

def claude_stream(path):
    rows = read_jsonl(path)
    init = next((r for r in rows if r.get("type") == "system" and r.get("subtype") == "init"), {})
    result = next((r for r in reversed(rows) if r.get("type") == "result"), {})
    return rows, init, result


def find_claude_transcripts(session_ids, projects=None):
    found = []
    for sid in session_ids:
        if sid:
            found += glob.glob(str((projects or CLAUDE_PROJECTS) / "*" / f"{sid}.jsonl"))
    return sorted(set(found))


def claude_steps(rows):
    steps = []
    for row in rows:
        msg = row.get("message") or {}
        for block in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if block.get("type") == "tool_use":
                steps.append({"kind": "tool", "name": block.get("name"),
                              "input": json.dumps(block.get("input"), ensure_ascii=False)[:400]})
            elif block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content or [] if isinstance(c, dict))
                steps.append({"kind": "result", "text": text[:300]})
            elif block.get("type") == "text" and msg.get("role") == "assistant" and block.get("text"):
                steps.append({"kind": "say", "text": block["text"][:600]})
    return steps


def codex_steps(rows):
    steps, final = [], ""
    for row in rows:
        item = row.get("item") or {}
        if row.get("type") != "item.completed":
            continue
        if item.get("type") == "command_execution":
            steps.append({"kind": "tool", "name": "shell", "input": (item.get("command") or "")[:400],
                          "exit": item.get("exit_code")})
            steps.append({"kind": "result", "text": (item.get("aggregated_output") or "")[:300]})
        elif item.get("type") == "agent_message":
            final = item.get("text") or ""
            steps.append({"kind": "say", "text": final[:600]})
    return steps, final


# ------------------------------------------------------------------- stages

def run_candidate_stage(ctx, stage, prompt, resume=None):
    """One candidate call. Returns (receipt, session_id)."""
    cand, effort, root, out = ctx["cand"], ctx["effort"], ctx["root"], ctx["out"]
    wt = root / "wt"
    stdout_path, stderr_path = out / f"{stage}.stdout.jsonl", out / f"{stage}.stderr.txt"
    if cand["runtime"] == "claude":
        if ctx.get("implement"):
            argv, env = claude_impl_argv(cand["model"], effort, root), impl_env(root, ctx["key"], cand)
        else:
            argv = claude_argv(cand["model"], effort, append_file=instructions(wt), resume=resume)
            env = claude_env(cand["client"], ctx["key"])
        proc = run_process(argv, prompt, wt, env, ctx["timeout"], stdout_path, stderr_path)
        rows, init, result = claude_stream(stdout_path)
        return {"stage": stage, **proc, "argv": redact_argv(argv), "init_tools": init.get("tools"),
                "init_mcp": init.get("mcp_servers"), "init_model": init.get("model"),
                "is_error": result.get("is_error"), "terminal_reason": result.get("terminal_reason"),
                "final": result.get("result") or "", "session_id": result.get("session_id") or init.get("session_id")
                }, result.get("session_id") or init.get("session_id")
    argv = codex_argv(cand["model"], effort, root, wt, resume=resume)
    env = impl_env(root, ctx["key"], cand) if ctx.get("implement") else codex_env(root, root / "codex-home", ctx["key"])
    proc = run_process(argv, prompt, wt, env, ctx["timeout"], stdout_path, stderr_path)
    rows = read_jsonl(stdout_path)
    steps, final = codex_steps(rows)
    thread = next((r.get("thread_id") for r in rows if r.get("type") == "thread.started"), None)
    return {"stage": stage, **proc, "argv": redact_argv(argv), "final": final, "session_id": resume or thread}, \
        resume or thread


def run_reviewer(ctx, case_prompt, draft):
    rev, root, out = ctx["reviewer"], ctx["root"], ctx["out"]
    home = root / "reviewer-codex-home"
    write_codex_home(home, rev.get("client", "gateway"))
    prompt = T["reviewer.prompt"].format(task=case_prompt, draft=draft)
    argv = codex_argv(rev["model"], rev["effort"], root, root / "wt")
    env = codex_env(root, home, ctx["key"])
    proc = run_process(argv, prompt, root / "wt", env, ctx["timeout"],
                       out / "review.stdout.jsonl", out / "review.stderr.txt")
    rows = read_jsonl(out / "review.stdout.jsonl")
    _, review = codex_steps(rows)
    files = sorted(glob.glob(str(home / "sessions" / "**" / "rollout-*.jsonl"), recursive=True))
    for i, f in enumerate(files):
        shutil.copyfile(f, out / f"review-rollout-{i}.jsonl")
    usage, models, efforts, subagents = codex_usage_from_files(files)
    return {"stage": "review", **proc, "final": review, "models": models, "efforts": efforts, "usage": usage,
            "subagents": subagents}


def redact_argv(argv):
    return [a if len(a) < 400 else a[:60] + "…" for a in argv]


def collect_usage(ctx, session_ids):
    cand, root, out = ctx["cand"], ctx["root"], ctx["out"]
    if cand["runtime"] == "claude":
        # A sandboxed (write-capable) Claude keeps its transcripts in its own config dir.
        files = find_claude_transcripts(session_ids, root / "claude-config" / "projects" if ctx.get("implement") else None)
        rows = []
        for i, f in enumerate(files):
            shutil.copyfile(f, out / f"transcript-{i}.jsonl")
            rows += read_jsonl(f)
        usage, efforts = claude_transcript_usage(rows)
        return usage, sorted(usage), efforts, claude_steps(rows), bool(files)
    files = sorted(glob.glob(str(root / "codex-home" / "sessions" / "**" / "rollout-*.jsonl"), recursive=True))
    for i, f in enumerate(files):
        shutil.copyfile(f, out / f"rollout-{i}.jsonl")
    usage, models, efforts, subagents = codex_usage_from_files(files)
    ctx["subagents"] = subagents
    steps = []
    for stage in ("draft", "revise", "implement"):
        s, _ = codex_steps(read_jsonl(out / f"{stage}.stdout.jsonl"))
        steps += s
    return usage, models, efforts, steps, bool(files)


def expected_model(cand):
    return re.sub(r"\[.*\]$", "", cand["model"])


def validity(record, cand, effort, tools=CLAUDE_TOOLS):
    """A run counts only when the client proves what ran and everything finished."""
    reasons = []
    for stage in record["stages"]:
        if stage.get("timed_out"):
            reasons.append(f"{stage['stage']}: timed out")
        elif stage.get("returncode") not in (0, None):
            reasons.append(f"{stage['stage']}: exit {stage.get('returncode')}")
        if stage.get("is_error"):
            reasons.append(f"{stage['stage']}: client reported error ({stage.get('terminal_reason')})")
        if not (stage.get("final") or "").strip():
            reasons.append(f"{stage['stage']}: empty answer")
        if cand["runtime"] == "claude" and stage["stage"] != "review":
            if stage.get("init_tools") is not None and sorted(stage["init_tools"]) != sorted(tools.split(",")):
                reasons.append(f"{stage['stage']}: tools were {stage['init_tools']}")
            if stage.get("init_mcp"):
                reasons.append(f"{stage['stage']}: MCP servers were loaded")
    if not record["usage_found"] or not record["usage"]:
        reasons.append("no usage record")
    want = expected_model(cand)
    got = [re.sub(r"\[.*\]$", "", m) for m in record["actual_models"]]
    if got != [want]:
        reasons.append(f"model mismatch: expected {want}, recorded {record['actual_models']}")
    if cand["runtime"] == "codex" or cand["client"] == "native":
        if record["actual_efforts"] != [effort]:
            reasons.append(f"effort mismatch: requested {effort}, recorded {record['actual_efforts']}")
    elif record["actual_efforts"] and record["actual_efforts"] != [effort]:
        reasons.append(f"effort mismatch: requested {effort}, recorded {record['actual_efforts']}")
    if record.get("cost_usd") is None:
        reasons.append("cost unknown: model has no list price")
    return reasons


def adopt_prompt_extra(draft, review):
    """Forced adoption: the author may reject a review point only with repository evidence."""
    return T["candidate.adopt"].format(draft=draft, review=review)


def execute_run(run, batch, common, cases, arms, prices, key, timeout):
    case = cases[run["case"]]
    cand, effort, mode = run["candidate"], run["effort"], run["mode"]
    root = run_dir(batch, run["run_id"])
    out = batch_dir(batch) / "runs" / run["run_id"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    prepare_snapshot(case, root)
    prompt = build_prompt(case, common, run.get("suffix")) + run.get("prompt_extra", "")
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    prepare_sandbox(root)
    if cand["runtime"] == "codex":
        write_codex_home(root / "codex-home", cand.get("client", "gateway"))
    ctx = {"cand": cand, "effort": effort, "root": root, "out": out, "key": key, "timeout": timeout,
           "reviewer": arms["reviewer"]}
    markers = case.get("leak_markers", []) + common.get("leak_markers", [])
    if cand["runtime"] == "claude":
        env = claude_env(cand["client"], key)
        argv = claude_argv(cand["model"], effort, append_file=instructions(root / "wt"))
    else:
        env = codex_env(root, root / "codex-home", key)
        argv = codex_argv(cand["model"], effort, root, root / "wt")
    base = {"run_id": run["run_id"], "batch": batch, "source_run": run.get("source_run"), "case": case["id"], "snapshot": case["snapshot"],
            "case_digest": run.get("case_digest"), "overlay": overlay_files(), "candidate": cand["id"], "model": cand["model"],
            "runtime": cand["runtime"], "client": cand["client"], "effort": effort, "mode": mode, "timeout_s": timeout,
            "started_at": now()}
    failures = isolation_check(root, markers, prompt, env, cand["runtime"], cand["client"],
                               argv=argv)
    if failures:
        shutil.rmtree(root / "wt", ignore_errors=True)
        return {**base, "status": "invalid", "reasons": ["isolation: " + f for f in failures],
                "stages": [], "finished_at": now()}
    stages, sessions = [], []
    draft, sid = run_candidate_stage(ctx, "draft", prompt)
    stages.append(draft)
    sessions.append(sid)
    review = None
    if mode == "review" and not draft.get("timed_out") and (draft.get("final") or "").strip():
        review = run_reviewer(ctx, prompt, draft["final"])
        stages.append({k: v for k, v in review.items() if k != "usage"})
        if (review.get("final") or "").strip():
            revise_prompt = T["candidate.revise"].format(review=review["final"])
            revised, sid2 = run_candidate_stage(ctx, "revise", revise_prompt, resume=sid)
            stages.append(revised)
            sessions.append(sid2)
    usage, models, efforts, steps, found = collect_usage(ctx, sessions)
    (out / "trajectory.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1), encoding="utf-8")
    # Each snapshot is a full checkout; kept, a batch of them fills the disk.
    # Rollouts and homes stay for `recompute`; the snapshot is reproducible from the SHA.
    shutil.rmtree(root / "wt", ignore_errors=True)
    # A fresh CODEX_HOME unpacks ~90 MB of bundled files into .tmp; sessions stay for `recompute`.
    for home in ("codex-home", "reviewer-codex-home"):
        shutil.rmtree(root / home / ".tmp", ignore_errors=True)
    record = {**base, "stages": stages, "usage": usage, "usage_found": found, "actual_models": models,
              "subagents": ctx.get("subagents", []),
              "actual_efforts": efforts, "review_usage": (review or {}).get("usage", {}),
              "draft": draft.get("final", ""), "final": stages[-1].get("final", "") if mode == "review" else draft.get("final", ""),
              "elapsed_s": round(sum(s.get("elapsed_s", 0) for s in stages), 1)}
    record["cost_usd"] = cost_usd(usage, prices)
    review_cost = cost_usd(record["review_usage"], prices) if record["review_usage"] else 0.0
    record["chain_cost_usd"] = None if record["cost_usd"] is None or review_cost is None else round(record["cost_usd"] + review_cost, 6)
    if mode == "review" and len(stages) < 3:
        reasons = ["review chain incomplete"]
    else:
        reasons = []
    reasons += validity(record, cand, effort)
    if review is not None and review.get("efforts") and review["efforts"] != [arms["reviewer"]["effort"]]:
        reasons.append(f"reviewer effort mismatch: {review['efforts']}")
    record.update({"status": "invalid" if reasons else "valid", "reasons": reasons, "finished_at": now()})
    return record


# ------------------------------------------------------------------- records

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def batch_dir(batch):
    return RESULTS / batch


# Commands that write a batch's ledgers or run directories.
BATCH_WRITERS = ("run", "adopt", "judge", "calibrate", "recompute")


def hold_batch(batch):
    """Lock the batch for this process's lifetime, or refuse: one process writes a batch at a time.

    Two writers of one batch share its run directories and scratch. On 2026-09-30 a second
    `run` of a batch, started seconds after the first, left every run it touched twice in the
    ledger and scored runs against worktrees the other process had reset. The lock is the
    open file returned; the operating system drops it when the process exits, however it ends.
    """
    d = batch_dir(batch)
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / ".lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        holder = fh.read().strip() or "another process"
        fh.close()
        raise EvalError(f"batch {batch} is being written by {holder}; one process writes a batch at a time") from None
    fh.truncate(0)
    fh.write(f"pid {os.getpid()} on {socket.gethostname()} since {now()}\n")
    fh.flush()
    return fh


def load_records(batch, name="runs.jsonl"):
    latest = {}
    for row in read_jsonl(batch_dir(batch) / name):
        latest[row.get("key") or row["run_id"]] = row
    return latest


class RecordWriter:
    """The single writer of a batch ledger: one appended JSON line per terminal state.

    Each row is stamped with the text, the kit, the host entry and the sandbox it ran on; one
    batch may hold rows from several machines. `recompute` rewrites rows run earlier and passes
    stamp=False, so an old row never claims today's text or machine.
    """

    def __init__(self, path, stamp=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stamp = stamp

    def append(self, row):
        if self.stamp:
            row = {**row, "text_digest": TEXT_DIGEST, "kit_digest": KIT_DIGEST, "host": HOST, "sandbox": SANDBOX}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self.lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


LEDGERS = ("runs.jsonl", "plans.jsonl", "judgments.jsonl", "calibration.jsonl")


def check_batch_text(batch):
    """One batch compares candidates on one text: refuse to add to a batch written with another.

    Rows written before digests existed carry none, so such a batch stays read-only: `report`
    works, `run`, `adopt`, `judge` and `calibrate` refuse it.
    """
    found = {row.get("text_digest") for name in LEDGERS for row in read_jsonl(batch_dir(batch) / name)}
    if found - {TEXT_DIGEST}:
        seen = ", ".join(sorted(d or "none" for d in found))
        raise EvalError(f"batch {batch} was written with prompt text {seen}; the configured text is {TEXT_DIGEST}. "
                        "Start a new batch: one batch compares candidates on one text.")


def cmd_run(args):
    check_batch_text(args.batch)
    common, cases = load_cases(args.cases)
    check_calibrated(cases, args.dry_run)
    impl_cases = {k: v for k, v in cases.items() if is_impl(v)}
    if impl_cases:
        if len(impl_cases) != len(cases):
            raise EvalError("run planning and implementation cases in separate batches (--cases)")
        return cmd_run_impl(args, common, impl_cases)
    arms, prices = load_arms(), load_prices()
    runs = expand_runs(cases, arms, args.modes, args.candidates, args.efforts, args.repeats)
    done = {k for k, v in load_records(args.batch).items() if v.get("status") == "valid"} if not args.rerun else set()
    todo = [r for r in runs if r["run_id"] not in done]
    print(f"batch {args.batch}: {len(runs)} runs, {len(runs) - len(todo)} already valid, {len(todo)} to run")
    if args.dry_run:
        for r in todo:
            print(r["run_id"])
        return 0
    key = gateway_key()
    writer = RecordWriter(batch_dir(args.batch) / "runs.jsonl")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(execute_run, r, args.batch, common, cases, arms, prices, key,
                               args.timeout or DEFAULT_TIMEOUT): r for r in todo}
        for fut in concurrent.futures.as_completed(futures):
            run = futures[fut]
            try:
                record = fut.result()
            except Exception as exc:  # a crashed run is recorded, never silently dropped
                record = {"run_id": run["run_id"], "batch": args.batch, "case": run["case"],
                          "case_digest": run.get("case_digest"), "candidate": run["candidate"]["id"],
                          "model": run["candidate"]["model"], "effort": run["effort"], "mode": run["mode"], "status": "invalid",
                          "reasons": [f"runner crashed: {type(exc).__name__}: {exc}"], "finished_at": now()}
            writer.append(record)
            cost = record.get("chain_cost_usd")
            print(f"{record['status']:7} {record['run_id']:32} ${cost if cost is not None else '?'} "
                  f"{record.get('elapsed_s', '?')}s {'; '.join(record.get('reasons', []))[:160]}", flush=True)
    return 0


def cmd_recompute(args):
    """Re-derive codex usage, cost and validity from the saved rollouts; appends a corrected record.

    Needed for records written before per-file aggregation: they counted only the last
    rollout file, so a run whose candidate or reviewer spawned subagents was under-costed.
    Planning runs only: an implementation run's cost includes its shared plan and its validity
    its hidden tests, which this does not re-derive, so such a batch is refused untouched.
    """
    implement = sorted(r["run_id"] for r in load_records(args.batch).values() if r.get("kind") == "implement")
    if implement:
        raise EvalError(f"recompute re-derives planning runs only; batch {args.batch} holds implementation runs "
                        f"({', '.join(implement[:3])}{', …' if len(implement) > 3 else ''})")
    arms, prices = load_arms(), load_prices()
    cands = {c["id"]: c for c in arms["candidates"]}
    writer = RecordWriter(batch_dir(args.batch) / "runs.jsonl", stamp=False)
    changed = 0
    for rec in list(load_records(args.batch).values()):
        if not rec.get("stages"):
            continue
        out = batch_dir(args.batch) / "runs" / rec["run_id"]
        cand = cands[rec["candidate"]]
        new = dict(rec)
        if cand["runtime"] == "codex":
            files = sorted(glob.glob(str(out / "rollout-*.jsonl")))
            usage, models, efforts, subagents = codex_usage_from_files(files)
            new.update(usage=usage, actual_models=models, actual_efforts=efforts, subagents=subagents,
                       usage_found=bool(files))
        if rec["mode"] == "review":
            review_files = sorted(glob.glob(str(out / "review-rollout-*.jsonl")))
            if not review_files:
                home = run_dir(args.batch, rec["run_id"]) / "reviewer-codex-home"
                review_files = sorted(glob.glob(str(home / "sessions" / "**" / "rollout-*.jsonl"), recursive=True))
                for i, f in enumerate(review_files):
                    shutil.copyfile(f, out / f"review-rollout-{i}.jsonl")
            if review_files:
                ru, rm, re_, rs = codex_usage_from_files(review_files)
                new["review_usage"] = ru
                for st in new["stages"]:
                    if st["stage"] == "review":
                        st.update(models=rm, efforts=re_, subagents=rs)
        new["cost_usd"] = cost_usd(new["usage"], prices)
        review_cost = cost_usd(new.get("review_usage") or {}, prices) if new.get("review_usage") else 0.0
        new["chain_cost_usd"] = None if new["cost_usd"] is None or review_cost is None else round(new["cost_usd"] + review_cost, 6)
        reasons = ["review chain incomplete"] if rec["mode"] == "review" and len(new["stages"]) < 3 else []
        reasons += validity(new, cand, rec["effort"])
        review_stage = next((st for st in new["stages"] if st["stage"] == "review"), None)
        if review_stage and review_stage.get("efforts") and review_stage["efforts"] != [arms["reviewer"]["effort"]]:
            reasons.append(f"reviewer effort mismatch: {review_stage['efforts']}")
        new.update(status="invalid" if reasons else "valid", reasons=reasons, recomputed_at=now())
        if (new["status"], new.get("chain_cost_usd")) != (rec.get("status"), rec.get("chain_cost_usd")):
            changed += 1
            print(f"{rec['run_id']:32} {rec.get('status')}->{new['status']} "
                  f"${rec.get('chain_cost_usd')}->${new.get('chain_cost_usd')}")
        writer.append(new)
    print(f"{changed} records changed")
    return 0


def adopt_runs(source_records, candidates, efforts, repeats):
    """One forced-adoption run per earlier review run and repeat, reusing its draft and review.

    A repeated source (`…-review-r2`) keeps its repeat in the new id (`…-adopt-s2-r1`), so two
    sources never claim one run directory; an unrepeated source keeps the old `…-adopt-r1`.
    """
    runs = []
    for rec in sorted(source_records, key=lambda r: r["run_id"]):
        if rec.get("mode") != "review" or rec.get("status") != "valid":
            continue
        if candidates and rec["candidate"] not in candidates or efforts and rec["effort"] not in efforts:
            continue
        review = next((s.get("final") or "" for s in rec["stages"] if s["stage"] == "review"), "")
        if not review.strip() or not (rec.get("draft") or "").strip():
            continue
        source_rep = re.search(r"-review-r(\d+)$", rec["run_id"])
        stem = f"{rec['case']}-{rec['candidate']}-{rec['effort']}-adopt" + (f"-s{source_rep.group(1)}" if source_rep else "")
        for rep in range(1, repeats + 1):
            runs.append({"run_id": f"{stem}-r{rep}",
                         "candidate_id": rec["candidate"], "case": rec["case"], "effort": rec["effort"], "mode": "adopt",
                         "source_run": rec["run_id"], "prompt_extra": adopt_prompt_extra(rec["draft"], review)})
    return runs


def cmd_adopt(args):
    check_batch_text(args.batch)
    common, cases = load_cases(args.cases)
    check_calibrated(cases, args.dry_run)
    arms, prices = load_arms(), load_prices()
    cands = {c["id"]: c for c in arms["candidates"]}
    source = [r for r in load_records(args.from_batch).values() if r["case"] in cases]
    runs = adopt_runs(source, args.candidates, args.efforts, args.repeats)
    for r in runs:
        r["candidate"] = cands[r["candidate_id"]]
        r["case_digest"] = cases[r["case"]]["digest"]
    done = {k for k, v in load_records(args.batch).items() if v.get("status") == "valid"}
    todo = [r for r in runs if r["run_id"] not in done]
    print(f"batch {args.batch}: {len(runs)} adopt runs from {args.from_batch}, {len(todo)} to run")
    if args.dry_run:
        for r in todo:
            print(r["run_id"], "<-", r["source_run"])
        return 0
    key = gateway_key()
    writer = RecordWriter(batch_dir(args.batch) / "runs.jsonl")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(execute_run, r, args.batch, common, cases, arms, prices, key,
                               args.timeout or DEFAULT_TIMEOUT): r for r in todo}
        for fut in concurrent.futures.as_completed(futures):
            run = futures[fut]
            try:
                record = fut.result()
            except Exception as exc:
                record = {"run_id": run["run_id"], "batch": args.batch, "case": run["case"],
                          "case_digest": run.get("case_digest"), "candidate": run["candidate"]["id"],
                          "model": run["candidate"]["model"], "effort": run["effort"], "mode": "adopt", "source_run": run["source_run"],
                          "status": "invalid", "reasons": [f"runner crashed: {type(exc).__name__}: {exc}"],
                          "finished_at": now()}
            writer.append(record)
            cost = record.get("chain_cost_usd")
            print(f"{record['status']:7} {record['run_id']:32} ${cost if cost is not None else '?'} "
                  f"{record.get('elapsed_s', '?')}s {'; '.join(record.get('reasons', []))[:160]}", flush=True)
    return 0


def cmd_check_isolation(args):
    """Standalone isolation check for one case; `--plant` adds an answer file to prove the check fails."""
    common, cases = load_cases([args.case])
    case = cases[args.case]
    root = SCRATCH_ROOT / "_isolation" / args.case
    prepare_snapshot(case, root)
    prepare_sandbox(root)
    markers = case.get("leak_markers", []) + common.get("leak_markers", [])
    if args.plant:
        (root / "home" / "notes.md").write_text(f"answer: {markers[0]}\n", encoding="utf-8")
    prompt = build_prompt(case, common)
    env = codex_env(root, root / "codex-home", "placeholder")
    argv = codex_argv("check-isolation", "none", root, root / "wt")
    failures = isolation_check(root, markers, prompt, env, "codex", "gateway", argv=argv, implement=is_impl(case))
    print(json.dumps({"case": args.case, "planted": args.plant, "host": HOST, "sandbox": SANDBOX,
                      "pass": not failures, "failures": failures}, ensure_ascii=False, indent=1))
    return 0 if not failures else 1


# ------------------------------------------------------------ implementation

def is_impl(case):
    return case.get("kind") == "implement"


def impl_cfg():
    impl = CFG.get("implement")
    if not impl:
        raise EvalError("config has no 'implement' section")
    return impl


def claude_impl_argv(model, effort, root):
    """Write-capable Claude: the whole CLI runs inside the sandbox of run root `root`.

    Tool permissions are skipped because the sandbox, not the prompt, is the boundary:
    reads and writes outside the run directory fail at the kernel.
    """
    return sandboxed(root, [str(CLAUDE_BIN), "-p", "--model", model,
                            "--effort", effort, "--tools", IMPL_TOOLS, "--dangerously-skip-permissions",
                            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format",
                            "stream-json", "--verbose"])


def impl_env(root, key, cand):
    """A fresh environment: the test venv first on PATH, the snapshot importable, no inherited secrets."""
    root = Path(root)
    path = [str(Path(TEST_PYTHON).parent)] if TEST_PYTHON else []
    if cand["runtime"] == "codex":
        path.append(os.path.dirname(shutil.which(CODEX_BIN) or CODEX_BIN))
    path += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {"PATH": os.pathsep.join(dict.fromkeys(path)), "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
           "LANG": "en_US.UTF-8", "PYTHONPATH": str(root / "wt"), "PYTHONDONTWRITEBYTECODE": "1"}
    if cand["runtime"] == "codex":
        env.update({"CODEX_HOME": str(root / "codex-home"), GATEWAY_KEY_ENV: key})
    else:
        if cand.get("client") != "gateway":
            raise EvalError(f"{cand['id']}: a write-capable Claude candidate must use the gateway; "
                            "a native login lives outside the sandbox")
        env.update({"CLAUDE_CODE_TMPDIR": str(root / "tmp"), "CLAUDE_CONFIG_DIR": str(root / "claude-config"),
                    "ANTHROPIC_BASE_URL": GATEWAY_URL, "ANTHROPIC_AUTH_TOKEN": key})
    return env


def expand_impl_runs(cases, impl, modes=None, candidates=None, efforts=None, repeats=None):
    runs = []
    for case_id, case in cases.items():
        for cand in impl["candidates"]:
            if candidates and cand["id"] not in candidates:
                continue
            for effort in cand["efforts"]:
                if efforts and effort not in efforts:
                    continue
                for mode in case.get("modes", impl["modes"]):
                    if modes and mode not in modes:
                        continue
                    for rep in range(1, (repeats or impl.get("repeats", 1)) + 1):
                        runs.append({"run_id": f"{case_id}-{cand['id']}-{effort}-{mode}-r{rep}", "case": case_id,
                                     "candidate": cand, "effort": effort, "mode": mode, "rep": rep,
                                     "case_digest": case.get("digest")})
    return runs


def plan_run_id(case_id, planner_id, rep):
    return f"{case_id}-plan-{planner_id}-r{rep}"


def planner_of(mode):
    return mode[len("split-"):] if mode.startswith("split-") else None


def plans_needed(runs):
    """One plan per (case, planner, repeat): every implementer of that repeat gets the same plan."""
    return sorted({(r["case"], planner_of(r["mode"]), r["rep"]) for r in runs if planner_of(r["mode"])})


def ensure_plans(batch, runs, common, cases, impl, prices, key, timeout, parallel):
    """Each (case, planner, repeat) plan is written once and shared by every implementer."""
    planners = {p["id"]: p for p in impl.get("planners", [])}
    wanted = plans_needed(runs)
    have = {k for k, v in load_records(batch, "plans.jsonl").items() if v.get("status") == "valid"}
    todo = []
    for case_id, pid, rep in wanted:
        if pid not in planners:
            raise EvalError(f"mode split-{pid}: no planner '{pid}' in implement.planners")
        rid = plan_run_id(case_id, pid, rep)
        if rid not in have:
            p = planners[pid]
            todo.append({"run_id": rid, "case": case_id, "candidate": {**p, "efforts": [p["effort"]]},
                         "effort": p["effort"], "mode": "solo", "suffix": common["planner_suffix"],
                         "case_digest": cases[case_id].get("digest")})
    if todo:
        print(f"plans: {len(wanted)} needed, {len(todo)} to write", flush=True)
        _run_pool(todo, lambda r: execute_run(r, batch, common, cases, load_arms(), prices, key, timeout),
                  RecordWriter(batch_dir(batch) / "plans.jsonl"), batch, parallel)
    return load_records(batch, "plans.jsonl")


def capture_diff(wt, base, out):
    """The candidate's whole change against the snapshot commit, committed or not."""
    git = ["git", "-C", str(wt), "-c", "core.quotepath=off"]
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    diff = subprocess.run([*git, "diff", "--cached", base], capture_output=True, text=True).stdout
    numstat = subprocess.run([*git, "diff", "--cached", "--numstat", base], capture_output=True, text=True).stdout
    (out / "diff.patch").write_text(diff, encoding="utf-8")
    stat = {"files": 0, "added": 0, "deleted": 0, "test_files": 0}
    for line in numstat.splitlines():
        added, deleted, path = line.split("\t", 2)
        stat["files"] += 1
        stat["test_files"] += int(path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_"))
        stat["added"] += int(added) if added.isdigit() else 0
        stat["deleted"] += int(deleted) if deleted.isdigit() else 0
    return stat


def parse_junit(path, expected, reference_only=()):
    """Counts and per-test outcomes of a hidden-test run; reference-only tests are not scored.

    A test id is junit's `classname::name` (a collection error reports the module alone).
    `hidden_tests.reference_only` lists tests that encode the real fix's own design: they run
    and are reported as `reference_only_passed`, but `passed`, the other counts and
    `scored_expected` leave them out.
    """
    import xml.etree.ElementTree as ET
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    reference_only = set(reference_only)
    scored_expected = expected - len(reference_only)
    base = {"expected": expected, "scored_expected": scored_expected, "reference_only_passed": 0}
    try:
        tree = ET.parse(path)
    except (FileNotFoundError, ET.ParseError):
        return {**counts, **base, "pass_rate": 0.0, "junit": False, "cases": {}}
    outcomes, reference_passed = {}, 0
    for tc in tree.iter("testcase"):
        tags = {child.tag for child in tc}
        key = ("failed" if "failure" in tags else "errors" if "error" in tags
               else "skipped" if "skipped" in tags else "passed")
        test_id = f"{tc.get('classname')}::{tc.get('name')}" if tc.get("classname") else tc.get("name")
        outcomes[test_id] = {"errors": "error"}.get(key, key)
        if test_id in reference_only:
            reference_passed += key == "passed"
            continue
        counts[key] += 1
    return {**counts, **base, "reference_only_passed": reference_passed,
            "pass_rate": round(counts["passed"] / scored_expected, 4) if scored_expected else None,
            "junit": True, "cases": outcomes}


def run_hidden_tests(case, root, out, timeout):
    """Place the fix's tests over the candidate's tree and run them in the sandbox.

    A file the candidate wrote at the same path is replaced: the hidden version is the
    acceptance contract. Collection errors (a missing module or name) count as zero passes
    for that file, measured against the expected total, never against what was collected;
    the other files still run (pytest otherwise stops the whole session at the first one).
    """
    wt, ht = Path(root) / "wt", case["hidden_tests"]
    for f in ht["files"]:
        target = wt / f["place"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(case["dir"] / f["file"], target)
    junit = Path(root) / "tmp" / "hidden-junit.xml"
    junit.unlink(missing_ok=True)
    argv = sandboxed(root, [TEST_PYTHON, "-m", "pytest", *ht["paths"], "-q", "-p", "no:cacheprovider",
                            "--continue-on-collection-errors", f"--junitxml={junit}"])
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path(root) / "home"), "TMPDIR": str(Path(root) / "tmp"),
           "LANG": "en_US.UTF-8", "PYTHONPATH": str(wt), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = run_process(argv, "", wt, env, timeout, out / "hidden-tests.stdout.txt", out / "hidden-tests.stderr.txt")
    if junit.exists():
        shutil.copyfile(junit, out / "hidden-junit.xml")
    return {"returncode": proc["returncode"], "elapsed_s": proc["elapsed_s"], "timed_out": proc["timed_out"],
            **parse_junit(junit, ht["expected"], ht.get("reference_only", []))}


def execute_impl_run(run, batch, common, cases, impl, prices, key, timeout, plans):
    case = cases[run["case"]]
    cand, effort, mode = run["candidate"], run["effort"], run["mode"]
    root = run_dir(batch, run["run_id"])
    out = batch_dir(batch) / "runs" / run["run_id"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    base = {"run_id": run["run_id"], "batch": batch, "kind": "implement", "case": case["id"],
            "snapshot": case["snapshot"], "case_digest": run.get("case_digest"), "overlay": overlay_files(),
            "candidate": cand["id"], "model": cand["model"], "runtime": cand["runtime"],
            "client": cand["client"], "effort": effort, "mode": mode, "rep": run["rep"], "timeout_s": timeout,
            "started_at": now()}
    # The leak check reads the task alone. A shared plan comes from a planner run that passed its
    # own isolation check, so a marker in it is the planner's derivation (a test file named by the
    # repository's convention), not a leak; it is recorded, not rejected.
    task_prompt = prompt = build_prompt(case, common)
    markers = case.get("leak_markers", []) + common.get("leak_markers", [])
    plan = None
    if planner_of(mode):
        plan = plans.get(plan_run_id(case["id"], planner_of(mode), run["rep"]))
        if not plan or plan.get("status") != "valid":
            return {**base, "status": "invalid", "reasons": ["plan unavailable"], "stages": [], "finished_at": now()}
        prompt += "\n\n" + common["split_plan_intro"] + "\n\n" + T["candidate.plan_heading"] + "\n" + plan["final"]
        base["plan_marker_hits"] = [m for m in markers if m and m in plan["final"]]
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    wt = prepare_snapshot(case, root)
    (root / "claude-config").mkdir()
    base_sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    prepare_sandbox(root)
    if cand["runtime"] == "codex":
        write_codex_home(root / "codex-home", cand.get("client", "gateway"))
    ctx = {"cand": cand, "effort": effort, "root": root, "out": out, "key": key, "timeout": timeout,
           "implement": True}
    argv = (claude_impl_argv(cand["model"], effort, root) if cand["runtime"] == "claude"
            else codex_argv(cand["model"], effort, root, wt))
    failures = isolation_check(root, markers, task_prompt, impl_env(root, key, cand), cand["runtime"], cand["client"],
                               argv=argv, implement=True)
    if failures:
        shutil.rmtree(root / "wt", ignore_errors=True)
        return {**base, "status": "invalid", "reasons": ["isolation: " + f for f in failures],
                "stages": [], "finished_at": now()}
    stage, sid = run_candidate_stage(ctx, "implement", prompt)
    usage, models, efforts, steps, found = collect_usage(ctx, [sid])
    (out / "trajectory.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1), encoding="utf-8")
    diff = capture_diff(wt, base_sha, out)
    tests = run_hidden_tests(case, root, out, impl.get("test_timeout", 900))
    shutil.rmtree(root / "wt", ignore_errors=True)
    shutil.rmtree(root / "codex-home" / ".tmp", ignore_errors=True)
    record = {**base, "stages": [stage], "usage": usage, "usage_found": found, "actual_models": models,
              "actual_efforts": efforts, "subagents": ctx.get("subagents", []), "final": stage.get("final", ""),
              "diff": diff, "tests": tests, "plan_run": plan and plan["run_id"],
              "plan_cost_usd": plan and plan.get("chain_cost_usd"), "elapsed_s": stage.get("elapsed_s")}
    record["cost_usd"] = cost_usd(usage, prices)
    plan_cost = record["plan_cost_usd"] or 0.0
    record["chain_cost_usd"] = None if record["cost_usd"] is None else round(record["cost_usd"] + plan_cost, 6)
    reasons = validity(record, cand, effort, tools=IMPL_TOOLS)
    if not tests["junit"] and not tests["timed_out"]:
        reasons.append("hidden tests produced no report")
    record.update({"status": "invalid" if reasons else "valid", "reasons": reasons, "finished_at": now()})
    return record


def _run_pool(runs, fn, writer, batch, parallel, budget=None):
    """Run in parallel, write each terminal state once; stop launching once `budget` dollars are spent."""
    spent = 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(fn, r): r for r in runs}
        for fut in concurrent.futures.as_completed(futures):
            run = futures[fut]
            if fut.cancelled():
                continue
            try:
                record = fut.result()
            except Exception as exc:  # a crashed run is recorded, never silently dropped
                record = {"run_id": run["run_id"], "batch": batch, "case": run["case"],
                          "case_digest": run.get("case_digest"), "candidate": run["candidate"]["id"],
                          "model": run["candidate"]["model"], "effort": run["effort"], "mode": run["mode"], "status": "invalid",
                          "reasons": [f"runner crashed: {type(exc).__name__}: {exc}"], "finished_at": now()}
            writer.append(record)
            cost = record.get("chain_cost_usd")
            spent += (record.get("cost_usd") or 0.0)
            tests = record.get("tests")
            tail = f" tests {tests['passed']}/{tests.get('scored_expected', tests['expected'])}" if tests else ""
            print(f"{record['status']:7} {record['run_id']:40} ${cost if cost is not None else '?'} "
                  f"{record.get('elapsed_s', '?')}s{tail} {'; '.join(record.get('reasons', []))[:160]}", flush=True)
            if budget is not None and spent >= budget:
                cancelled = sum(f.cancel() for f in futures)
                print(f"budget ${budget} reached (${spent:.2f}); {cancelled} queued runs not started", flush=True)
                budget = None


def impl_timeout(impl, effort, given=None):
    """An implementer's time limit: `--timeout` if given, else `implement.timeouts[effort]`, else the default.

    One limit for every effort cut the high efforts short on Q (four runs at 267–269 tests).
    """
    return given or (impl.get("timeouts") or {}).get(effort, DEFAULT_TIMEOUT)


def cmd_run_impl(args, common, cases):
    impl, prices = impl_cfg(), load_prices()
    runs = expand_impl_runs(cases, impl, args.modes, args.candidates, args.efforts, args.repeats)
    done = {k for k, v in load_records(args.batch).items() if v.get("status") == "valid"} if not args.rerun else set()
    todo = [r for r in runs if r["run_id"] not in done]
    print(f"batch {args.batch} (implement): {len(runs)} runs, {len(runs) - len(todo)} already valid, {len(todo)} to run")
    if args.dry_run:
        for case_id, pid, rep in plans_needed(todo):
            print("plan", plan_run_id(case_id, pid, rep))
        for r in todo:
            print(r["run_id"])
        return 0
    key = gateway_key()
    plans = ensure_plans(args.batch, todo, common, cases, impl, prices, key, args.timeout or DEFAULT_TIMEOUT,
                         args.parallel)
    _run_pool(todo, lambda r: execute_impl_run(r, args.batch, common, cases, impl, prices, key,
                                               impl_timeout(impl, r["effort"], args.timeout), plans),
              RecordWriter(batch_dir(args.batch) / "runs.jsonl"), args.batch, args.parallel, args.budget)
    return 0


def alt_failures(reference_cases, alt_cases, reference_only=(), accepted=()):
    """Tests the reference passes and an alternative implementation does not, less the excused ones.

    An alternative is a reasonable implementation that follows another plan. A test it fails
    must be reference-only (it encodes the real fix's own design) or an accepted genuine defect
    of that patch; anything else is a test that punishes a reasonable choice.
    """
    excused = set(reference_only) | set(accepted)
    return sorted(t for t, outcome in reference_cases.items()
                  if outcome == "passed" and alt_cases.get(t) != "passed" and t not in excused)


def cmd_calibrate_tests(args):
    """Hidden tests fail on the bare snapshot, pass in full with the real fix, and fail an
    alternative implementation only where the case says why; the lint must show no error."""
    common, cases = load_cases([args.case])
    case = cases[args.case]
    if not is_impl(case):
        raise EvalError(f"{args.case} is not an implementation case")
    lint = lint_case(case, common)
    print_lint(lint)
    ht = case["hidden_tests"]
    reference_only = ht.get("reference_only", [])
    alts = case.get("alt_patches") or []
    root = SCRATCH_ROOT / "_calibrate_tests" / args.case
    out = root / "out"
    results, problems = {"alts": []}, []
    arms = [("bare", None), ("reference", case["reference_patch"])] + [("alt", a) for a in alts]
    for arm, patch in arms:
        wt = prepare_snapshot(case, root)
        out.mkdir(parents=True, exist_ok=True)
        if arm == "reference":
            subprocess.run(["git", "-C", str(wt), "apply", str(case["dir"] / patch)], check=True)
        elif arm == "alt":
            applied = subprocess.run(["git", "-C", str(wt), "apply", str(case["dir"] / patch["file"])],
                                     capture_output=True, text=True)
            if applied.returncode != 0:
                results["alts"].append({"file": patch["file"], "applied": False, "error": applied.stderr[-300:]})
                problems.append(f"alt patch {patch['file']} does not apply")
                continue
        prepare_sandbox(root)
        got = run_hidden_tests(case, root, out, impl_cfg().get("test_timeout", 900))
        if arm != "alt":
            results[arm] = got
            continue
        accepted = patch.get("accepted_failures") or {}
        unexcused = alt_failures(results["reference"]["cases"], got["cases"], reference_only, accepted)
        results["alts"].append({"file": patch["file"], "note": patch.get("note", ""), "applied": True,
                                "passed": got["passed"], "scored_expected": got["scored_expected"],
                                "unexcused_failures": unexcused, "cases": got["cases"],
                                "accepted_but_passed": sorted(t for t in accepted if got["cases"].get(t) == "passed")})
        if unexcused:
            problems.append(f"alt patch {patch['file']} fails {len(unexcused)} tests the case does not excuse: "
                            + ", ".join(unexcused[:5]) + (", …" if len(unexcused) > 5 else ""))
    if "baseline" in ht and results["bare"]["passed"] != ht["baseline"]:
        print(f"note: bare snapshot passed {results['bare']['passed']}, case records baseline "
              f"{ht['baseline']}", file=sys.stderr)
    ref = results["reference"]
    if results["bare"]["passed"] >= ref["scored_expected"]:
        problems.append("the bare snapshot already passes every scored test")
    if ref["passed"] != ref["scored_expected"] or ref["reference_only_passed"] != len(reference_only):
        problems.append(f"the reference passes {ref['passed']}/{ref['scored_expected']} scored and "
                        f"{ref['reference_only_passed']}/{len(reference_only)} reference-only tests")
    if not alts:
        problems.append("no alt_patches: at least one alternative implementation must show the tests accept "
                        "another reasonable design")
    if lint["errors"]:
        problems.append(f"lint-case: {len(lint['errors'])} errors")
    ok = not problems
    shutil.rmtree(root, ignore_errors=True)
    digest = case_digest(case)
    results.update(lint=lint, problems=problems)
    write_calibration("tests", args.case, digest, {"ok": ok, **results})
    brief = {k: ({kk: vv for kk, vv in v.items() if kk != "cases"} if isinstance(v, dict) and k != "lint" else v)
             for k, v in results.items() if k != "lint"}
    brief["alts"] = [{k: v for k, v in a.items() if k != "cases"} for a in results["alts"]]
    print(json.dumps({"case": args.case, "case_digest": digest, "pass": ok, **brief}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


def impl_answer(rec, out, limit=120_000):
    """What the judges read for an implementation run: the closing message and the diff.

    Source files come before test files so a long diff is cut in its tests first; any
    file left out is named, never silently dropped.
    """
    diff = (out / "diff.patch").read_text(encoding="utf-8") if (out / "diff.patch").exists() else ""
    parts = [p for p in re.split(r"(?m)^(?=diff --git )", diff) if p.strip()]

    def is_test(part):
        path = part.split("\n", 1)[0].split(" b/")[-1]
        return path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith("test_")
    parts.sort(key=is_test)
    kept, omitted, size = [], [], 0
    for part in parts:
        if size + len(part) > limit:
            omitted.append(part.split("\n", 1)[0].split(" b/")[-1])
            continue
        kept.append(part)
        size += len(part)
    note = T["judge.diff_omitted"].format(files=", ".join(omitted)) if omitted else ""
    heading = T["judge.diff_heading"].format(files=rec["diff"]["files"], added=rec["diff"]["added"],
                                             deleted=rec["diff"]["deleted"])
    return f"{rec.get('final', '')}\n\n{heading}\n" + ("".join(kept) or T["judge.no_changes"]) + note


# ----------------------------------------------------------------- case lint

# pytest fixtures whose attributes belong to pytest, not to the candidate's code.
PYTEST_FIXTURES = {"caplog", "capsys", "capfd", "capsysbinary", "capfdbinary", "monkeypatch", "tmp_path",
                   "tmp_path_factory", "tmpdir", "tmpdir_factory", "request", "recwarn", "pytestconfig"}


def _library_attrs():
    """Attribute names of builtin and common stdlib objects a test calls on its own values."""
    import logging
    import pathlib
    import unittest.mock
    names = set(dir(builtins))
    for obj in (str, bytes, list, dict, set, frozenset, tuple, int, float, complex, bool, object, type,
                BaseException, logging.LogRecord, pathlib.Path, re.Match, re.Pattern, unittest.mock.Mock):
        names.update(dir(obj))
    return names


def _finding(check, where, value):
    return {"check": check, "where": where, "value": value}


def _worth_checking(literal):
    """Wording a candidate would have to guess: not an identifier, a number or a ≤3-character ASCII token."""
    t = literal.strip()
    if not t or (t.isascii() and (t.isidentifier() or len(t) <= 3)):
        return False
    try:
        float(t)
        return False
    except ValueError:
        return True


def _root_name(node):
    """The Name an attribute, subscript or call chain starts from, or None."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _module_id(place):
    return Path(place).with_suffix("").as_posix().replace("/", ".")


def disclosed_text(case, common):
    """Everything the candidate reads: the built prompt and each attachment placed in the snapshot."""
    parts = [build_prompt(case, common)]
    parts += [(case["dir"] / att["file"]).read_text(encoding="utf-8", errors="replace")
              for att in case.get("attachments", []) if att.get("place")]
    return "\n".join(parts)


def _disclosed(value, text):
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", text) is not None


class _HiddenFile:
    """One hidden test file's AST: what it asserts, what it uses and what it defines itself."""

    def __init__(self, label, place, source, repo_tops):
        self.label, self.place = label, place
        self.tree = ast.parse(source)
        self.imports = {}  # alias -> top-level module ("." for a relative import)
        self.imported = []  # (line, name) imported from a repository module
        self.literals, self.uses, self.kw_uses = [], [], []  # (line, value)
        self.fed = set()  # string constants the file supplies itself, outside the literal positions
        self.attr_defs, self.top_defs, self.classes = set(), set(), set()
        self.tests = []  # (node id, FunctionDef)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name.split(".")[0]] = a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                top = "." if node.level else (node.module or "").split(".")[0]
                for a in node.names:
                    self.imports[a.asname or a.name] = top
                    if top == "." or top in repo_tops:
                        self.imported.append((node.lineno, a.name))
        self.library = {alias for alias, top in self.imports.items() if top != "." and top not in repo_tops}
        self._definitions()
        self._tests()

    def _definitions(self):
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.top_defs.add(node.name)
            if isinstance(node, ast.ClassDef):
                self.classes.add(node.name)
            for target in _assigned(node):
                self.top_defs.add(target)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        self.attr_defs.add(item.name)
                    self.attr_defs.update(_assigned(item))
            elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                  and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")):
                self.attr_defs.add(node.attr)
            elif isinstance(node, ast.Dict):
                self.attr_defs.update(k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
        self.attr_defs |= self.top_defs

    def _tests(self):
        module = _module_id(self.place)
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                self.tests.append((f"{module}::{node.name}", node))
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        self.tests.append((f"{module}.{node.name}::{item.name}", item))

    def scan(self, local_callables, local_classes):
        """Literal assertions and used names; `local_callables` are defined by some hidden file.

        A keyword passed to a builtin, a library or a hidden file's own class defines an attribute
        (`SimpleNamespace(uid=1)`); one passed to a hidden file's function is that function's
        parameter and defines nothing the code under test must have.
        """
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assert):
                for cmp in (n for n in ast.walk(node.test) if isinstance(n, ast.Compare)):
                    operands = [cmp.left, *cmp.comparators]
                    for i, op in enumerate(cmp.ops):
                        sides = ([operands[i]] if isinstance(op, (ast.In, ast.NotIn))
                                 else operands[i:i + 2] if isinstance(op, (ast.Eq, ast.NotEq)) else [])
                        self.literals += [(s.lineno, s.value, False, id(s)) for s in sides
                                          if isinstance(s, ast.Constant) and isinstance(s.value, str)]
            elif isinstance(node, ast.Call):
                self._scan_call(node, local_callables, local_classes)
            elif isinstance(node, ast.Attribute):
                root = _root_name(node)
                self_store = isinstance(node.ctx, ast.Store) and root in ("self", "cls")
                if not self_store and root not in self.library and root not in PYTEST_FIXTURES \
                        and not node.attr.startswith("__"):
                    self.uses.append((node.lineno, node.attr))
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str) and node.slice.value.strip() \
                    and "\n" not in node.slice.value:
                root = _root_name(node)
                if root not in self.library and root not in PYTEST_FIXTURES:
                    self.uses.append((node.lineno, node.slice.value))
        asserted = {lit[3] for lit in self.literals}
        self.fed = {n.value for n in ast.walk(self.tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in asserted}

    def _scan_call(self, node, local_callables, local_classes):
        func = node.func
        pytest_call = (isinstance(func, ast.Attribute) and func.attr in ("raises", "warns")
                       and isinstance(func.value, ast.Name) and self.imports.get(func.value.id) == "pytest") or \
                      (isinstance(func, ast.Name) and func.id in ("raises", "warns") and self.imports.get(func.id) == "pytest")
        if pytest_call:
            self.literals += [(kw.value.lineno, kw.value.value, True, id(kw.value)) for kw in node.keywords
                              if kw.arg == "match" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)]
            return
        root = _root_name(func)
        # A call on a local value (`stack.evaluate(x=1)`) still names a parameter of the code under test.
        direct = isinstance(func, ast.Name)
        if root in self.library or root in PYTEST_FIXTURES or (direct and (root in local_classes
                                                                            or hasattr(builtins, root))):
            self.attr_defs.update(kw.arg for kw in node.keywords if kw.arg)
            return
        if direct and root in local_callables:
            return
        self.kw_uses += [(node.lineno, kw.arg) for kw in node.keywords if kw.arg]


def _assigned(node):
    """Names a statement binds directly: assignment and annotated-assignment targets."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
    out = []
    for t in targets:
        out += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
    return out


_SNAPSHOT_WORDS = {}


def _snapshot_words(snapshot):
    """The snapshot's text files (≤2 MB each, binaries skipped) and the set of words in them.

    Read with one ls-tree and one cat-file call. `git grep -o -w -f <names>` was the first
    design; over a 239 MB snapshot with common names it ran for minutes (measured 2026-09-29),
    since it prints every occurrence of every name.
    """
    if snapshot not in _SNAPSHOT_WORDS:
        ls = subprocess.run(["git", "-C", str(REPO), "ls-tree", "-r", "-l", snapshot], capture_output=True, text=True)
        if ls.returncode != 0:
            raise EvalError(f"snapshot {snapshot[:9]} is not in {REPO}: {ls.stderr[-300:]}")
        oids = []
        for line in ls.stdout.splitlines():
            meta, _ = line.split("\t", 1)
            _, kind, oid, size = meta.split()
            if kind == "blob" and size.isdigit() and int(size) <= 2_000_000:
                oids.append(oid)
        out = subprocess.run(["git", "-C", str(REPO), "cat-file", "--batch"], input=("\n".join(oids) + "\n").encode(),
                             capture_output=True, check=True).stdout
        texts, pos = [], 0
        while pos < len(out):
            head_end = out.index(b"\n", pos)
            size = int(out[pos:head_end].split()[2])
            body = out[head_end + 1:head_end + 1 + size]
            pos = head_end + 1 + size + 1
            if b"\0" not in body:
                texts.append(body)
        text = b"\n".join(texts)
        _SNAPSHOT_WORDS[snapshot] = (text, set(re.findall(rb"[A-Za-z0-9_]+", text)))
    return _SNAPSHOT_WORDS[snapshot]


def snapshot_names(snapshot, names):
    """Which of `names` occur as whole words (git grep -w's sense) anywhere in the snapshot."""
    names = {n for n in names if n and "\n" not in n}
    if not names:
        return set()
    text, words = _snapshot_words(snapshot)
    found = set()
    for name in names:
        raw = name.encode("utf-8")
        if raw in words or (not re.fullmatch(rb"[A-Za-z0-9_]+", raw) and re.search(
                rb"(?<![A-Za-z0-9_])" + re.escape(raw) + rb"(?![A-Za-z0-9_])", text)):
            found.add(name)
    return found


def repo_tops(snapshot):
    proc = subprocess.run(["git", "-C", str(REPO), "ls-tree", "--name-only", snapshot], capture_output=True, text=True)
    if proc.returncode != 0:
        raise EvalError(f"snapshot {snapshot[:9]} is not in {REPO}: {proc.stderr[-300:]}")
    return {Path(n).stem if n.endswith(".py") else n for n in proc.stdout.split()}


def _hidden_files(case, tops):
    out = []
    for f in (case.get("hidden_tests") or {}).get("files", []):
        source = (case["dir"] / f["file"]).read_text(encoding="utf-8")
        try:
            out.append(_HiddenFile(f["file"], f["place"], source, tops))
        except SyntaxError as exc:
            out.append({"file": f["file"], "line": exc.lineno})
    return out


def lint_case(case, common):
    """Case checks a calibration cannot see. Errors fail `lint-case` and `calibrate-tests`.

    Implementation cases: every wording a hidden test asserts, and every name it uses that the
    snapshot lacks, must appear in what the candidate reads; for a given-plan case the tests
    touching an undisclosed name are listed with their docstrings for comparison with the plan.
    Every case: trap.pass clauses cite existing checks, trap.direction avoids the configured
    phrases, and no attachment or background line matches trap.wrong_markers.
    """
    errors, warnings, docstrings = [], [], []
    trap = case.get("trap") or {}
    text = disclosed_text(case, common)
    test_ids = set()
    if is_impl(case):
        tops = repo_tops(case["snapshot"]) | {Path(f["place"]).parts[0] for f in case["hidden_tests"]["files"]}
        files = _hidden_files(case, tops)
        errors += [_finding("syntax", f"{f['file']}:{f['line']}", "") for f in files if isinstance(f, dict)]
        files = [f for f in files if not isinstance(f, dict)]
        local_callables = set().union(*(f.top_defs for f in files)) if files else set()
        local_classes = set().union(*(f.classes for f in files)) if files else set()
        for f in files:
            f.scan(local_callables, local_classes)
            test_ids.update(tid for tid, _ in f.tests)
        defined = set().union(*(f.attr_defs for f in files)) if files else set()
        # Wording the tests feed in themselves (a broker's error text passed through) is not guessed.
        fed = set().union(*(f.fed for f in files)) if files else set()
        unshown = []  # (file, line, literal)
        for f in files:
            for line, literal, is_regex, _ in sorted(f.literals, key=lambda lit: lit[0]):
                if not _worth_checking(literal) or literal in fed:
                    continue
                try:
                    shown = re.search(literal, text) is not None if is_regex else literal in text
                except re.error:
                    shown = literal in text
                if not shown:
                    errors.append(_finding("literal", f"{f.label}:{line}", literal))
                    unshown.append((f, line, literal))
        occurrences = {}
        for f in files:
            for line, name in f.uses + f.kw_uses + f.imported:
                occurrences.setdefault(name, []).append((f, line))
        candidates = {n for n in occurrences if n not in defined} - _library_attrs()
        candidates = {n for n in candidates if not _disclosed(n, text)}
        missing = sorted(candidates - snapshot_names(case["snapshot"], candidates))
        for name in missing:
            where = sorted((f.label, line) for f, line in occurrences[name])
            more = f" {T['lint.more'].format(n=len(where) - 1)}" if len(where) > 1 else ""
            errors.append(_finding("name", f"{where[0][0]}:{where[0][1]}{more}", name))
        if "given-plan" in case.get("modes", []):
            for f in files:
                for tid, fn in f.tests:
                    inside = lambda g, line: g is f and fn.lineno <= line <= fn.end_lineno  # noqa: E731
                    touched = sorted({n for n in missing for g, line in occurrences[n] if inside(g, line)}
                                     | {lit for g, line, lit in unshown if inside(g, line)})
                    if touched:
                        docstrings.append({"test": tid, "where": f"{f.label}:{fn.lineno}", "names": touched,
                                           "docstring": ast.get_docstring(fn) or ""})
    rubric_ids = {r["id"] for r in rubric_for(case, common)}
    if isinstance(trap.get("pass", ""), str):
        warnings.append(_finding("pass_string", "trap.pass", ""))
    else:
        for i, clause in enumerate(pass_clauses(trap), 1):
            cited = clause.get("checked_by") or []
            if not cited:
                warnings.append(_finding("unmapped", f"trap.pass ({i})", clause.get("text", "")[:80]))
            for cid in cited:
                if cid not in rubric_ids and cid.split("[")[0] not in test_ids:
                    errors.append(_finding("checked_by", f"trap.pass ({i})", cid))
    for phrase in (CFG.get("lint") or {}).get("direction_forbidden", []):
        if phrase and phrase in (trap.get("direction") or ""):
            warnings.append(_finding("direction", "trap.direction", phrase))
    sources = [(f"background:{i}", line) for i, line in enumerate(case.get("background", []), 1)]
    for att in case.get("attachments", []):
        lines = (case["dir"] / att["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        sources += [(f"{att['file']}:{n}", line) for n, line in enumerate(lines, 1)]
    for pattern in trap.get("wrong_markers", []):
        try:
            rx = re.compile(pattern)
        except re.error:
            errors.append(_finding("bad_regex", "trap.wrong_markers", pattern))
            continue
        warnings += [_finding("wrong_marker", where, pattern) for where, line in sources if rx.search(line)]
    return {"case": case["id"], "errors": errors, "warnings": warnings, "docstrings": docstrings}


def print_lint(result):
    for level, key in (("errors", "lint.error"), ("warnings", "lint.warning")):
        for f in result[level]:
            value = f" {f['value']!r}" if f["value"] else ""
            print(f"{T[key]} {f['where']}: {T['lint.' + f['check']]}{value}")
    if result["docstrings"]:
        print(T["lint.docstrings"])
        for d in result["docstrings"]:
            print(f"  {d['test']} ({d['where']}; {', '.join(d['names'])}): {d['docstring'].splitlines()[0] if d['docstring'] else ''}")
    if not result["errors"] and not result["warnings"]:
        print(T["lint.clean"])


def cmd_lint_case(args):
    common, cases = load_cases([args.case])
    result = lint_case(cases[args.case], common)
    print_lint(result)
    return 1 if result["errors"] else 0


# --------------------------------------------------------------------- judge

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "trap": {"type": "object", "properties": {"direction": {"type": "boolean"}, "pass": {"type": "boolean"},
                                                  "evidence": {"type": "string"}},
                 "required": ["direction", "pass", "evidence"], "additionalProperties": False},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "pass": {"type": "boolean"}, "evidence": {"type": "string"}},
            "required": ["id", "pass", "evidence"], "additionalProperties": False}},
    },
    "required": ["trap", "items"],
    "additionalProperties": False,
}


def case_kind(case):
    return "implement" if is_impl(case) else "plan"


def rubric_for(case, common):
    """The case's rubric and the shared one, less items whose `kinds` exclude this case's kind.

    A shared item such as "attempted no side effects" is a planning rule; an implementer
    following an approved plan changes configuration by design (C1 on Q, 2026-09-29).
    """
    kind = case_kind(case)
    return [r for r in case.get("rubric", []) + common.get("rubric", []) if kind in r.get("kinds", [kind])]


def excluded_rubric_ids(case, common):
    kind = case_kind(case)
    return {r["id"] for r in case.get("rubric", []) + common.get("rubric", []) if kind not in r.get("kinds", [kind])}


def pass_clauses(trap):
    """`trap.pass` as a list of clauses: a legacy string is one clause without `checked_by`."""
    value = trap.get("pass", "")
    return [{"text": value}] if isinstance(value, str) else list(value)


def trap_pass_text(trap):
    """A string pass as written; a clause list numbered, one clause per line."""
    if isinstance(trap.get("pass", ""), str):
        return trap.get("pass", "")
    return "\n".join(f"({i}) {c['text']}" for i, c in enumerate(pass_clauses(trap), 1))


def judge_prompt(case, common, prompt, steps, answer):
    rubric = rubric_for(case, common)
    traj = "\n".join(
        f"[{i}] {s['kind']}: {s.get('name', '')} {s.get('input', '')}{s.get('text', '')}".strip()
        for i, s in enumerate(steps, 1)) or T["judge.no_steps"]
    items = "\n".join(T["judge.rubric_item"].format(id=r["id"], text=r["text"], evidence=r["evidence"]) for r in rubric)
    return T["judge.prompt"].format(
        prompt=prompt, description=case["trap"]["description"], direction=case["trap"]["direction"],
        trap_pass=trap_pass_text(case["trap"]), items=items, equivalents=case["equivalents"], trajectory=traj,
        answer=answer, ids=", ".join(r["id"] for r in rubric))


def call_judge(judge, prompt, workdir, key, timeout):
    workdir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if judge["runtime"] == "claude":
        argv = claude_argv(judge["model"], judge["effort"], tools="", schema=JUDGE_SCHEMA)
        env = claude_env(judge["client"], key)
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=workdir, env=env,
                              timeout=timeout)
        try:
            envelope = json.loads(proc.stdout)
        except ValueError:
            raise EvalError(f"judge {judge['id']} returned no JSON: {proc.stderr[-400:]}")
        if envelope.get("is_error") or not envelope.get("structured_output"):
            raise EvalError(f"judge {judge['id']} error: {envelope.get('result', '')[:300]}")
        usage = {judge["model"]: {"input": envelope.get("usage", {}).get("input_tokens", 0),
                                  "output": envelope.get("usage", {}).get("output_tokens", 0),
                                  "cache_read": envelope.get("usage", {}).get("cache_read_input_tokens", 0),
                                  "cache_write_5m": envelope.get("usage", {}).get("cache_creation_input_tokens", 0)}}
        return envelope["structured_output"], usage, round(time.monotonic() - started, 1)
    home = workdir / "codex-home"
    write_codex_home(home, judge.get("client", "gateway"))
    (workdir / "tmp").mkdir(exist_ok=True)
    (workdir / "home").mkdir(exist_ok=True)
    schema_path, out_path = workdir / "schema.json", workdir / "verdict.json"
    schema_path.write_text(json.dumps(JUDGE_SCHEMA), encoding="utf-8")
    prepare_sandbox(workdir)
    argv = codex_argv(judge["model"], judge["effort"], workdir, workdir, schema_path=schema_path,
                      out_path=out_path, sandbox="read-only")
    env = codex_env(workdir, home, key)
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=workdir, env=env, timeout=timeout)
    if not out_path.exists():
        raise EvalError(f"judge {judge['id']} wrote no verdict: {proc.stderr[-400:]}")
    verdict = json.loads(out_path.read_text(encoding="utf-8"))
    usage, _, _, _ = codex_usage_from_files(glob.glob(str(home / "sessions" / "**" / "rollout-*.jsonl"), recursive=True))
    return verdict, usage, round(time.monotonic() - started, 1)


def judge_targets(record):
    """Draft and revision are scored separately so the reviewer's contribution is visible."""
    if record["mode"] == "review":
        return [("draft", record.get("draft", "")), ("final", record.get("final", ""))]
    return [("final", record.get("final", ""))]


def cmd_judge(args):
    check_batch_text(args.batch)
    common, cases = load_cases(args.cases)
    arms, prices = load_arms(), load_prices()
    key = gateway_key()
    runs = [r for r in load_records(args.batch).values()
            if r.get("status") == "valid" and r["case"] in cases
            and (not args.candidates or r["candidate"] in args.candidates)
            and (not args.efforts or r["effort"] in args.efforts)
            and (not args.modes or r["mode"] in args.modes)]
    done = set(load_records(args.batch, "judgments.jsonl"))
    writer = RecordWriter(batch_dir(args.batch) / "judgments.jsonl")
    jobs = []
    for rec in runs:
        out = batch_dir(args.batch) / "runs" / rec["run_id"]
        prompt = (out / "prompt.txt").read_text(encoding="utf-8")
        steps = json.loads((out / "trajectory.json").read_text())
        targets = [("final", impl_answer(rec, out))] if rec.get("kind") == "implement" else judge_targets(rec)
        for target, answer in targets:
            # A draft is judged on the steps before the review arrived; the trajectory file is ordered.
            for judge in arms["judges"]:
                k = f"{rec['run_id']}|{target}|{judge['id']}"
                if k in done and not args.rerun:
                    continue
                jobs.append((k, rec, target, judge, judge_prompt(cases[rec["case"]], common, prompt, steps, answer)))
    print(f"{len(jobs)} judgments to run")

    def work(job):
        k, rec, target, judge, prompt = job
        workdir = SCRATCH_ROOT / args.batch / "_judge" / k.replace("|", "__")
        if workdir.exists():
            shutil.rmtree(workdir)
        verdict, usage, elapsed = call_judge(judge, prompt, workdir, key, args.timeout)
        shutil.rmtree(workdir, ignore_errors=True)  # verdict and usage are in the ledger row
        return {"key": k, "run_id": rec["run_id"], "target": target, "judge": judge["id"], "verdict": verdict,
                "usage": usage, "cost_usd": cost_usd(usage, prices), "elapsed_s": elapsed, "at": now()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(work, j): j for j in jobs}
        for fut in concurrent.futures.as_completed(futures):
            k = futures[fut][0]
            try:
                row = fut.result()
            except Exception as exc:
                row = {"key": k, "error": f"{type(exc).__name__}: {exc}"[:500], "at": now()}
            writer.append(row)
            print(("ERROR " if "error" in row else "ok    ") + k, flush=True)
    return 0


def cmd_calibrate(args):
    """Both judges must fail each case's known negative and pass its known positive.

    The negative is the original agent's answer; failing it shows the judges can fail.
    The positive is the answer the owner accepted after the correction; passing it shows
    the Trap is passable as written. A judge that fails the positive is too strict, or the
    pass condition asks for more than the owner did. A case without a positive fails:
    a Trap nobody can pass looks the same as models that all fail. Each case's result is
    recorded under its digest, and `run` launches only on a passing record.
    """
    check_batch_text(args.batch)
    common, cases = load_cases(args.cases)
    arms, prices = load_arms(), load_prices()
    key = gateway_key()
    writer = RecordWriter(batch_dir(args.batch) / "calibration.jsonl")
    ok = True
    for case in cases.values():
        digest = case_digest(case)
        if not case.get("calibration_positive"):
            write_calibration("judges", case["id"], digest, {"ok": False, "batch": args.batch, "judges": {},
                                                            "reason": "no calibration_positive"})
            print(f"{case['id']} no positive: a trap nobody can pass looks like models that all fail  CALIBRATION FAIL")
            ok = False
            continue
        prompt = build_prompt(case, common)
        samples = [("negative", case["calibration_negative"], False),
                   ("positive", case["calibration_positive"], True)]
        levels_by_judge, case_ok = {}, True
        for kind, rel, expect in samples:
            answer = (case["dir"] / rel).read_text(encoding="utf-8")
            for judge in arms["judges"]:
                workdir = SCRATCH_ROOT / args.batch / "_calibrate" / f"{case['id']}-{kind}-{judge['id']}"
                if workdir.exists():
                    shutil.rmtree(workdir)
                verdict, usage, elapsed = call_judge(judge, judge_prompt(case, common, prompt, [], answer),
                                                     workdir, key, args.timeout)
                shutil.rmtree(workdir, ignore_errors=True)
                trap = verdict["trap"]
                levels = (bool(trap.get("direction")), bool(trap["pass"]))
                good = levels == (expect, expect)
                case_ok = case_ok and good
                levels_by_judge.setdefault(judge["id"], {})[kind] = {"direction": levels[0], "pass": levels[1],
                                                                     "ok": good}
                writer.append({"key": f"{case['id']}|{kind}|{judge['id']}", "case": case["id"], "judge": judge["id"],
                               "kind": kind, "expected": expect, "verdict": verdict, "case_digest": digest,
                               "cost_usd": cost_usd(usage, prices), "elapsed_s": elapsed, "at": now()})
                print(f"{case['id']} {kind:8} {judge['id']:12} direction={levels[0]} pass={levels[1]} "
                      f"{'OK' if good else 'CALIBRATION FAIL'}")
        write_calibration("judges", case["id"], digest, {"ok": case_ok, "batch": args.batch,
                                                        "judges": levels_by_judge})
        ok = ok and case_ok
    return 0 if ok else 1


# -------------------------------------------------------------------- report

def summarize(batch, cases, arms, common=None):
    """One row per run with each judge's verdicts and evidence.

    Rubric items the case's kind excludes are dropped. An implementation row written before
    per-test outcomes were recorded gets them from the run's saved junit report.
    """
    records = load_records(batch)
    judgments = load_records(batch, "judgments.jsonl")
    rows = []
    for rec in records.values():
        case = cases.get(rec.get("case"))
        excluded = excluded_rubric_ids(case, common) if case and common else set()
        row = {k: rec.get(k) for k in ("run_id", "case", "candidate", "model", "effort", "mode", "status",
                                        "reasons", "cost_usd", "chain_cost_usd", "elapsed_s", "kind", "rep",
                                        "tests", "diff", "plan_run", "plan_cost_usd", "overlay", "host", "sandbox",
                                        "case_digest", "timeout_s")}
        row["timed_out"] = any(s.get("timed_out") for s in rec.get("stages") or [])
        junit = batch_dir(batch) / "runs" / rec["run_id"] / "hidden-junit.xml"
        if row["tests"] and "cases" not in row["tests"] and junit.exists():
            row["tests"] = {**row["tests"], "cases": parse_junit(junit, row["tests"].get("expected", 0))["cases"]}
        row["scores"] = {}
        targets = [("final", None)] if rec.get("kind") == "implement" else judge_targets(rec)
        for target, _ in targets if rec.get("status") == "valid" else []:
            per_judge = {}
            for judge in arms["judges"]:
                j = judgments.get(f"{rec['run_id']}|{target}|{judge['id']}")
                if j and "verdict" in j:
                    v = j["verdict"]
                    kept = [i for i in v["items"] if i["id"] not in excluded]
                    per_judge[judge["id"]] = {"trap": v["trap"]["pass"], "direction": v["trap"].get("direction"),
                                              "items": {i["id"]: i["pass"] for i in kept},
                                              "evidence": {i["id"]: i.get("evidence", "") for i in kept},
                                              "trap_evidence": v["trap"]["evidence"]}
            row["scores"][target] = per_judge
        rows.append(row)
    return rows


def agreement(per_judge):
    """Both judges must agree for a pass; disagreement is surfaced for the owner."""
    if len(per_judge) < 2:
        return None, None, []
    traps = {j: s["trap"] for j, s in per_judge.items()}
    trap = True if all(traps.values()) else (False if not any(traps.values()) else None)
    ids = sorted({i for s in per_judge.values() for i in s["items"]})
    agreed, disputed = 0, []
    for i in ids:
        votes = [s["items"].get(i) for s in per_judge.values()]
        if all(v is True for v in votes):
            agreed += 1
        elif any(v is True for v in votes):
            disputed.append(i)
    return trap, (agreed, len(ids)), disputed


def cmd_report(args):
    common, cases = load_cases(None)
    arms = load_arms()
    rows = summarize(args.batch, cases, arms, common)
    out = Path(args.out) if args.out else batch_dir(args.batch) / "report.html"
    out.write_text(render_report(args.batch, rows, cases, arms, common), encoding="utf-8")
    (batch_dir(args.batch) / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                                        encoding="utf-8")
    print(out)
    if args.md:
        Path(args.md).write_text(render_markdown(args.batch, rows, cases, arms, common), encoding="utf-8")
        print(args.md)
    return 0


def _verdict(per):
    """(direction, pass) agreed by every judge: True, False, or None when they split or are missing."""
    if len(per) < 2:
        return None, None
    def agree(values):
        return True if all(values) else (False if not any(values) else None)
    directions = [s.get("direction") for s in per.values()]
    # Batches judged before the two-level Trap carry no direction at all: say so, not "failed".
    direction = "—" if all(d is None for d in directions) else agree(directions)
    return direction, agree([s["trap"] for s in per.values()])


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def disputes_of(run_id, target, per, case, common, trap_head):
    """Each rubric item and Trap level the judges split on, with every judge's verdict and evidence.

    `trap_head` is the heading of a Trap split (it names the run and, for plans, the target).
    Returns [(heading, [one line per judge])].
    """
    if not per:
        return []
    mark = {True: "✓", False: "✗", None: "?"}
    head = run_id if target is None else f"{run_id} {target}"
    texts = {r["id"]: r["text"] for r in (case or {}).get("rubric", []) + (common or {}).get("rubric", [])}
    out = []
    for item in agreement(per)[2]:
        out.append((T["report.dispute_item"].format(head=head, item=item, text=_clip(texts.get(item, ""), 80)),
                    [T["report.dispute_verdict"].format(
                        judge=j, verdict=T["report.pass"] if s["items"].get(item) else T["report.fail"],
                        evidence=_clip((s.get("evidence") or {}).get(item, ""), 200)) for j, s in per.items()]))
    d, t = _verdict(per)
    if d is None or t is None:
        out.append((trap_head, [T["report.dispute_trap_verdict"].format(
            judge=j, direction=mark.get(s.get("direction"), "?"), passed=mark.get(s["trap"], "?"),
            evidence=_clip(s.get("trap_evidence"), 200)) for j, s in per.items()]))
    return out


def discrimination(case, rows, reference_cases=None):
    """What the hidden tests can tell apart on this case's valid runs.

    From the per-test outcomes: the tests the reference passes that no candidate passed, the
    reachable ceiling (scored tests less those), the candidates' range of passed tests, and
    whether that range is below `implement.min_spread` (default 3), in which case the case does
    not discriminate between candidates. Without the calibration record's reference outcomes,
    the tests are those the runs reported, collection errors of a whole file left out.
    """
    ht = case["hidden_tests"]
    reference_only = set(ht.get("reference_only", []))
    scored = ht["expected"] - len(reference_only)
    matrix = [r["tests"]["cases"] for r in rows if (r.get("tests") or {}).get("cases")]
    if reference_cases:
        universe = {t for t, outcome in reference_cases.items() if outcome == "passed"} - reference_only
    else:
        universe = {t for cases in matrix for t in cases if "::" in t} - reference_only
    never = sorted(t for t in universe if matrix and not any(cases.get(t) == "passed" for cases in matrix))
    passed = [r["tests"]["passed"] for r in rows if r.get("tests")]
    lo, hi = (min(passed), max(passed)) if passed else (None, None)
    min_spread = (CFG.get("implement") or {}).get("min_spread", 3)
    return {"never_passed": never, "ceiling": scored - len(never) if matrix else None, "scored_expected": scored,
            "range": (lo, hi), "min_spread": min_spread, "reference_only": len(reference_only),
            "no_discrimination": bool(passed) and hi - lo < min_spread}


def reference_outcomes(case_id, rows, case):
    """The reference's per-test outcomes from the tests calibration the rows ran under, if recorded."""
    digests = sorted({r.get("case_digest") for r in rows if r.get("case_digest")})
    if case.get("dir"):
        digests.append(case_digest(case))
    for digest in digests:
        rec = read_calibration("tests", case_id, digest)
        if rec and (rec.get("reference") or {}).get("cases"):
            return rec["reference"]["cases"]
    return None


def discrimination_notes(case_id, case, rows):
    """The caption lines under an implementation case's table."""
    info = discrimination(case, rows, reference_outcomes(case_id, rows, case))
    lines = []
    if info["reference_only"]:
        lines.append(T["report.reference_only"].format(n=info["reference_only"]))
    if info["ceiling"] is not None:
        lines.append(T["report.ceiling"].format(ceiling=info["ceiling"], scored=info["scored_expected"],
                                                lo=info["range"][0], hi=info["range"][1]))
    if info["never_passed"]:
        lines.append(T["report.never_passed"].format(n=len(info["never_passed"]),
                                                     tests=T["report.list_sep"].join(info["never_passed"])))
    if info["no_discrimination"]:
        lines.append(T["report.no_discrimination"].format(spread=info["range"][1] - info["range"][0],
                                                          min_spread=info["min_spread"]))
    return lines


def timed_out_groups(case_id, rows):
    """Timed-out implementation runs with test results, by arm: they stay invalid but keep their evidence."""
    groups, sizes = {}, {}
    for r in rows:
        if r["case"] != case_id:
            continue
        key = (r["candidate"], r["model"], r["effort"], r["mode"])
        sizes[key] = sizes.get(key, 0) + 1
        if r["status"] != "valid" and r.get("timed_out") and (r.get("tests") or {}).get("junit"):
            groups.setdefault(key, []).append(r)
    return {k: (sorted(v, key=lambda r: r.get("rep") or 0), sizes[k]) for k, v in groups.items()}


def spend(rows):
    """List-price cost of these runs, a shared plan counted once."""
    plans = {r["plan_run"]: r.get("plan_cost_usd") or 0 for r in rows if r.get("plan_run")}
    return (sum(r.get("chain_cost_usd") or 0 for r in rows if not r.get("plan_run"))
            + sum(r.get("cost_usd") or 0 for r in rows if r.get("plan_run")) + sum(plans.values()))


def case_order(cases, rows):
    """Ranking cases first, then baseline cases, which check an approved plan can be finished.

    Only cases this batch ran: a planning batch shows no empty implementation tables.
    """
    ran = {r["case"] for r in rows}
    cases = {k: c for k, c in cases.items() if k in ran}
    ranking = [(k, c) for k, c in cases.items() if c.get("role", "ranking") != "baseline"]
    return ranking, [(k, c) for k, c in cases.items() if c.get("role", "ranking") == "baseline"]


def _mean_cost_time(reps):
    costs = [r["chain_cost_usd"] for r in reps if r.get("chain_cost_usd") is not None]
    secs = [r["elapsed_s"] for r in reps if r.get("elapsed_s")]
    return ("$%.2f" % (sum(costs) / len(costs)) if costs else "?"), (f"{int(sum(secs) / len(secs))}" if secs else "?")


def render_markdown(batch, rows, cases, arms, common=None):
    """The archived form of a batch: the same tables as the HTML report, as Markdown.

    Written for `docs/agent-eval/`; the page's conclusion section is added by hand
    above these tables, and the numbers here are the evidence it cites.
    """
    effort_order = ["medium", "high", "xhigh", "max", "ultra"]
    mark = {True: "✓", False: "✗", None: "?", "—": "—"}
    valid = [r for r in rows if r["status"] == "valid"]
    invalid = [r for r in rows if r["status"] != "valid"]
    judgments = load_records(batch, "judgments.jsonl")
    # A shared plan is paid once, however many implementers used it.
    run_cost = spend(valid)
    judge_cost = sum(j.get("cost_usd") or 0 for j in judgments.values())
    out = [T["report.md_title"].format(batch=batch), "",
           T["report.md_intro"].format(valid=len(valid), invalid=len(invalid), run_cost=run_cost, judge_cost=judge_cost), ""]
    if invalid and spend(rows) - run_cost > 0.005:
        out += [T["report.invalid_cost"].format(cost=spend(rows) - run_cost), ""]
    overlaid = sorted({f for r in valid for f in (r.get("overlay") or {})})
    if overlaid:
        out += [T["report.md_overlay"].format(files=T["report.list_sep"].join(f"`{f}`" for f in overlaid)), ""]
    machines = {}
    for r in valid:
        key = (r.get("host") or T["report.none"], r.get("sandbox") or T["report.none"])
        machines[key] = machines.get(key, 0) + 1
    if len({host for host, _ in machines}) > 1:
        hosts = T["report.list_sep"].join(T["report.md_host_item"].format(host=h, sandbox=sb, runs=n)
                                          for (h, sb), n in sorted(machines.items()))
        out += [T["report.md_hosts"].format(hosts=hosts), ""]
    disputes = []
    ranking, baseline = case_order(cases, rows)
    for section, level in ((ranking, "###"), (baseline, "####")):
        shown = [(k, c) for k, c in section if any(r["case"] == k for r in valid)
                 or (is_impl(c) and timed_out_groups(k, rows))]
        if section is baseline and shown:
            out += [f"### {T['report.baseline_heading']}", "", T["report.baseline_note"], ""]
        for case_id, case in shown:
            case_rows = [r for r in valid if r["case"] == case_id]
            out += [f"{level} {case_id} · {case['title']}", ""]
            if is_impl(case):
                out += _markdown_impl_table(case_id, case, case_rows, rows, common, disputes, effort_order, mark)
            else:
                out += _markdown_plan_table(case, case_rows, common, disputes, effort_order, mark)
            out.append("")
    none = f"- {T['report.none']}"
    lines = []
    for head, verdicts in disputes:
        lines += [f"- {head}"] + [f"  - {v}" for v in verdicts]
    out += [T["report.md_disputes"], ""] + (lines or [none]) + [""]
    out += [T["report.md_invalid"], ""] + ([T["report.md_invalid_item"].format(run=r["run_id"], reasons="; ".join(r.get("reasons") or []))
                                          for r in invalid] or [none]) + [""]
    return "\n".join(out)


def _markdown_impl_table(case_id, case, case_rows, rows, common, disputes, effort_order, mark):
    ht = case["hidden_tests"]
    out = [T["report.md_hidden"].format(expected=ht["expected"], baseline=ht.get("baseline", "?")), "",
           "| " + " | ".join(T["report.impl_columns"]) + " |",
           "|---|---|---|---|---|---|---|---|"]
    groups = {}
    for r in case_rows:
        groups.setdefault((r["model"], r["effort"], r["mode"]), []).append(r)
    for key in sorted(groups, key=lambda k: (k[0], effort_order.index(k[1]), k[2])):
        reps = sorted(groups[key], key=lambda r: r.get("rep") or 0)
        traps, items = [], []
        for r in reps:
            per = r["scores"].get("final") or {}
            d, t = _verdict(per)
            traps.append(f"{mark[d]}/{mark[t]}")
            _, agreed, _ = agreement(per) if per else (None, None, [])
            items.append(f"{agreed[0]}/{agreed[1]}" if agreed else "—")
            disputes.extend(disputes_of(r["run_id"], None, per, case, common,
                                        T["report.md_dispute_trap"].format(run=r["run_id"])))
        cost, secs = _mean_cost_time(reps)
        out.append(f"| {key[0]} | {key[1]} | {key[2]} | {' · '.join(str(r['tests']['passed']) for r in reps)} | "
                   f"{' · '.join(traps)} | {' · '.join(items)} | {cost} | {secs}s |")
    timed = timed_out_groups(case_id, rows)
    for key in sorted(timed, key=lambda k: (k[1], effort_order.index(k[2]), k[3])):
        reps, size = timed[key]
        cost, secs = _mean_cost_time(reps)
        label = T["report.timed_out"].format(n=len(reps), m=size)
        out.append(f"| {key[1]} | {key[2]} | {key[3]} ({label}) | "
                   f"{' · '.join(str(r['tests']['passed']) for r in reps)} | — | — | {cost} | {secs}s |")
    notes = discrimination_notes(case_id, case, case_rows) if case_rows else []
    return out + ([""] + notes if notes else [])


def _markdown_plan_table(case, case_rows, common, disputes, effort_order, mark):
    out = ["| " + " | ".join(T["report.plan_columns"]) + " |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(case_rows, key=lambda r: (r["model"], effort_order.index(r["effort"]), r["mode"])):
        cells = []
        for target in ("draft", "final"):
            per = r["scores"].get(target) or (r["scores"].get("final") if r["mode"] != "review" else {}) or {}
            d, t = _verdict(per)
            _, agreed, _ = agreement(per) if per else (None, None, [])
            cells += [f"{mark[d]}/{mark[t]}", f"{agreed[0]}/{agreed[1]}" if agreed else "—"]
            if r["mode"] == "review" or target == "draft":
                disputes.extend(disputes_of(r["run_id"], target, per, case, common,
                                            T["report.md_dispute_target_trap"].format(run=r["run_id"], target=target)))
        if r["mode"] != "review":
            cells[2:4] = [T["report.same_as_draft"], T["report.same_as_draft"]]
        cost = r.get("chain_cost_usd")
        out.append(f"| {r['model']} | {r['effort']} | {r['mode']} | {' | '.join(cells)} | "
                   f"{'$%.2f' % cost if cost is not None else '?'} | {r.get('elapsed_s') or '?'}s |")
    return out


def render_impl_case(case_id, case, rows, all_rows=(), common=None, level="h2"):
    """One table per implementation case: each arm's repeats side by side, then their mean.

    Timed-out runs of an arm follow as their own row; they stay invalid but keep their tests.
    """
    esc = html.escape
    effort_order = ["medium", "high", "xhigh", "max", "ultra"]
    mark = {True: "✓", False: "✗", None: "?"}
    groups = {}
    for r in rows:
        groups.setdefault((r["candidate"], r["model"], r["effort"], r["mode"]), []).append(r)
    disputes = []
    out = [f"<{level}>{esc(case_id)} · {esc(case['title'])}</{level}>"
           "<p class='lede'>" + T["report.impl_lede"].format(expected=case["hidden_tests"]["expected"],
                                                              baseline=case["hidden_tests"].get("baseline", "?"))
           + "</p><div class='scroll'><table><thead><tr>"
           + "".join(f"<th>{c}</th>" for c in T["report.impl_html_columns"]) + "</tr></thead><tbody>"]
    for key in sorted(groups, key=lambda k: (k[0], effort_order.index(k[2]), k[3])):
        reps = sorted(groups[key], key=lambda r: r.get("rep") or 0)
        rates = [r["tests"]["pass_rate"] or 0.0 for r in reps]
        tests_txt = " · ".join(f"{r['tests']['passed']}" for r in reps)
        traps, items_txt = [], []
        for r in reps:
            per = r["scores"].get("final") or {}
            trap, items, disputed = agreement(per) if per else (None, None, [])
            dirs = [s.get("direction") for s in per.values()]
            direction = True if dirs and all(dirs) else (False if dirs and not any(dirs) else None)
            traps.append(f"{mark[direction]}/{mark[trap]}")
            items_txt.append(f"{items[0]}/{items[1]}" if items else "—")
            disputes.extend(disputes_of(r["run_id"], None, per, case, common,
                                        T["report.html_dispute_trap"].format(run=r["run_id"])))
        cost, secs = _mean_cost_time(reps)
        diff_txt = " · ".join(f"+{r['diff']['added']}/-{r['diff']['deleted']}" for r in reps)
        out.append(f"<tr><td>{esc(key[1])}</td><td>{esc(key[2])}</td><td>{esc(key[3])}</td>"
                   f"<td class='num'>{tests_txt}</td><td class='num'>{sum(rates) / len(rates):.0%}</td>"
                   f"<td>{' · '.join(traps)}</td><td>{' · '.join(items_txt)}</td><td class='num'>{diff_txt}</td>"
                   f"<td class='num'>{cost}</td><td class='num'>{secs}s</td></tr>")
    timed = timed_out_groups(case_id, all_rows)
    for key in sorted(timed, key=lambda k: (k[0], effort_order.index(k[2]), k[3])):
        reps, size = timed[key]
        cost, secs = _mean_cost_time(reps)
        rates = [r["tests"].get("pass_rate") or 0.0 for r in reps]
        diff_txt = " · ".join(f"+{r['diff']['added']}/-{r['diff']['deleted']}" for r in reps if r.get("diff"))
        label = T["report.timed_out"].format(n=len(reps), m=size)
        out.append(f"<tr class='muted'><td>{esc(key[1])}</td><td>{esc(key[2])}</td>"
                   f"<td>{esc(key[3])} <span class='warn'>{esc(label)}</span></td>"
                   f"<td class='num'>{' · '.join(str(r['tests']['passed']) for r in reps)}</td>"
                   f"<td class='num'>{sum(rates) / len(rates):.0%}</td><td>—</td><td>—</td>"
                   f"<td class='num'>{diff_txt}</td><td class='num'>{cost}</td><td class='num'>{secs}s</td></tr>")
    out.append("</tbody></table></div>")
    notes = discrimination_notes(case_id, case, rows) if rows else []
    out += [f"<p class='lede'>{esc(n)}</p>" for n in notes]
    return "".join(out), disputes


def render_report(batch, rows, cases, arms, common=None):
    esc = html.escape
    valid = [r for r in rows if r["status"] == "valid"]
    invalid = [r for r in rows if r["status"] != "valid"]
    effort_order = ["medium", "high", "xhigh", "max", "ultra"]
    body = []
    disputes = []
    ranking, baseline = case_order(cases, rows)
    for section, level in ((ranking, "h2"), (baseline, "h3")):
        if section is baseline and section:
            body.append(f"<h2>{T['report.baseline_heading']}</h2><p class='lede'>{T['report.baseline_note']}</p>")
        for case_id, case in section:
            if is_impl(case):
                html_part, case_disputes = render_impl_case(case_id, case, [r for r in valid if r["case"] == case_id],
                                                            rows, common, level)
                body.append(html_part)
                disputes += case_disputes
                continue
            body.append(f"<{level}>{esc(case_id)} · {esc(case['title'])}</{level}><div class='scroll'><table><thead><tr>"
                        + "".join(f"<th>{c}</th>" for c in T["report.plan_columns"]) + "</tr></thead><tbody>")
            case_rows = sorted([r for r in valid if r["case"] == case_id],
                               key=lambda r: (r["candidate"], effort_order.index(r["effort"]), r["mode"]))
            for r in case_rows:
                cells = []
                for target in ("draft", "final"):
                    per = r["scores"].get(target) or ({} if target == "draft" else {})
                    if target == "draft" and r["mode"] == "solo":
                        per = r["scores"].get("final", {})
                    trap, items, disputed = agreement(per) if per else (None, None, [])
                    trap_txt = {True: f"<span class='ok'>{T['report.pass']}</span>",
                                False: f"<span class='bad'>{T['report.fail']}</span>",
                                None: f"<span class='warn'>{T['report.split_unjudged']}</span>"}[trap]
                    item_txt = f"{items[0]}/{items[1]}" if items else "—"
                    if disputed:
                        item_txt += f" <span class='warn'>{T['report.split_items'].format(ids=','.join(disputed))}</span>"
                    if r["mode"] != "solo" or target == "draft":
                        disputes.extend(disputes_of(r["run_id"], target, per, case, common,
                                                    T["report.html_dispute_target_trap"].format(run=r["run_id"],
                                                                                                target=target)))
                    cells += [trap_txt, item_txt]
                if r["mode"] == "solo":
                    cells[2:4] = [T["report.same_as_draft"], T["report.same_as_draft"]]
                cost = r.get("chain_cost_usd")
                body.append(f"<tr><td>{esc(r['model'])}</td><td>{esc(r['effort'])}</td><td>{esc(r['mode'])}</td>"
                            + "".join(f"<td>{c}</td>" for c in cells)
                            + f"<td class='num'>{'$%.2f' % cost if cost is not None else '?'}</td>"
                            f"<td class='num'>{r.get('elapsed_s') or '?'}s</td></tr>")
            body.append("</tbody></table></div>")
    none = f"<li>{T['report.none']}</li>"
    inv = "".join(f"<li><code>{esc(r['run_id'])}</code>{T['report.html_invalid_sep']}{esc('; '.join(r.get('reasons') or []))}</li>"
                  for r in invalid) or none
    dis = "".join(f"<li>{esc(head)}<ul>{''.join(f'<li>{esc(v)}</li>' for v in verdicts)}</ul></li>"
                  for head, verdicts in disputes) or none
    invalid_cost = spend(rows) - spend(valid)
    cost_note = (f"<p class='lede'>{esc(T['report.invalid_cost'].format(cost=invalid_cost))}</p>"
                 if invalid and invalid_cost > 0.005 else "")
    return f"""<title>{T['report.title'].format(batch=esc(batch))}</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1b2130;--muted:#5d6577;--line:#dfe2e8;--ok:#1f7a4d;--bad:#a3303a;--warn:#9a5b00;--card:#fff}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#12151c;--fg:#e6e8ee;--muted:#9aa2b3;--line:#2c3342;--ok:#6cc99a;--bad:#ec8b93;--warn:#e0b060;--card:#1a1f29}}}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#12151c;--fg:#e6e8ee;--muted:#9aa2b3;--line:#2c3342;--ok:#6cc99a;--bad:#ec8b93;--warn:#e0b060;--card:#1a1f29}}
body{{background:var(--bg);color:var(--fg);font-family:{T['report.font']};line-height:1.6}}
.wrap{{max-width:1100px;margin:0 auto;padding-inline:16px;padding-block:24px}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:var(--card);font-size:14px}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}}
.num{{font-variant-numeric:tabular-nums;text-align:right}} .ok{{color:var(--ok)}} .bad{{color:var(--bad)}} .warn{{color:var(--warn)}}
.lede{{color:var(--muted);max-width:75ch;overflow-wrap:anywhere}} tr.muted td{{color:var(--muted)}}
</style>
<div class="wrap">
<h1>{T['report.heading'].format(batch=esc(batch))}</h1>
<p class="lede">{T['report.lede'].format(valid=len(valid), invalid=len(invalid))}</p>
{cost_note}
{''.join(body)}
<h2>{T['report.disputes']}</h2><ul>{dis}</ul>
<h2>{T['report.invalid']}</h2><ul>{inv}</ul>
</div>"""


# ---------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.environ.get("AGENT_EVAL_CONFIG", "agent-eval/config.json"),
                    help="eval config (default: $AGENT_EVAL_CONFIG or agent-eval/config.json)")
    ap.add_argument("--host", help="apply this `hosts` entry of the config (default: the one named like this machine)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    csv = lambda s: [x for x in s.split(",") if x]  # noqa: E731

    run = sub.add_parser("run", help="run candidates and record each run's terminal state")
    run.add_argument("--batch", required=True)
    run.add_argument("--cases", type=csv)
    run.add_argument("--candidates", type=csv)
    run.add_argument("--efforts", type=csv)
    run.add_argument("--modes", type=csv)
    run.add_argument("--parallel", type=int, default=4)
    run.add_argument("--timeout", type=int, help=f"seconds per candidate stage (default: implement.timeouts by effort "
                                                 f"for implementers, else {DEFAULT_TIMEOUT})")
    run.add_argument("--rerun", action="store_true", help="rerun runs that are already valid")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--repeats", type=int, help="repeats per arm (implementation default implement.repeats; planning default one run, ids without -rN)")
    run.add_argument("--budget", type=float, help="implementation cases: stop launching once this many dollars are spent")
    run.set_defaults(func=cmd_run)

    ct = sub.add_parser("calibrate-tests", help="implementation case: hidden tests fail bare, pass with the real fix")
    ct.add_argument("--case", required=True)
    ct.set_defaults(func=cmd_calibrate_tests)

    lint = sub.add_parser("lint-case", help="case checks: undisclosed wording and names in hidden tests, trap wording")
    lint.add_argument("--case", required=True)
    lint.set_defaults(func=cmd_lint_case)

    ado = sub.add_parser("adopt", help="forced adoption: rerun authors on earlier drafts and reviews")
    ado.add_argument("--batch", required=True)
    ado.add_argument("--from-batch", required=True)
    ado.add_argument("--cases", type=csv)
    ado.add_argument("--candidates", type=csv)
    ado.add_argument("--efforts", type=csv)
    ado.add_argument("--repeats", type=int, default=1)
    ado.add_argument("--parallel", type=int, default=4)
    ado.add_argument("--timeout", type=int, help=f"seconds per candidate stage (default {DEFAULT_TIMEOUT})")
    ado.add_argument("--dry-run", action="store_true")
    ado.set_defaults(func=cmd_adopt)

    rec = sub.add_parser("recompute", help="re-derive codex usage, cost and validity from saved rollouts")
    rec.add_argument("--batch", required=True)
    rec.set_defaults(func=cmd_recompute)

    iso = sub.add_parser("check-isolation", help="prepare one case and run the isolation check")
    iso.add_argument("--case", required=True)
    iso.add_argument("--plant", action="store_true", help="plant an answer file; the check must then fail")
    iso.set_defaults(func=cmd_check_isolation)

    jud = sub.add_parser("judge", help="score valid runs with both judges")
    jud.add_argument("--batch", required=True)
    jud.add_argument("--cases", type=csv)
    jud.add_argument("--candidates", type=csv)
    jud.add_argument("--efforts", type=csv)
    jud.add_argument("--modes", type=csv)
    jud.add_argument("--parallel", type=int, default=4)
    jud.add_argument("--timeout", type=int, default=1800)
    jud.add_argument("--rerun", action="store_true")
    jud.set_defaults(func=cmd_judge)

    cal = sub.add_parser("calibrate", help="judges must fail each case's known negative")
    cal.add_argument("--batch", required=True)
    cal.add_argument("--cases", type=csv)
    cal.add_argument("--timeout", type=int, default=1800)
    cal.set_defaults(func=cmd_calibrate)

    rep = sub.add_parser("report", help="render the reference report")
    rep.add_argument("--batch", required=True)
    rep.add_argument("--out")
    rep.add_argument("--md", help="also write the tables as Markdown, for archiving under docs/")
    rep.set_defaults(func=cmd_report)

    txt = sub.add_parser("text", help="print the text table (the configured file, else the defaults) as JSON, "
                                      "the starting point for a translation")
    txt.set_defaults(func=lambda args: print(json.dumps(T, ensure_ascii=False, indent=1)) or 0)

    args = ap.parse_args(argv)
    try:
        if args.cmd != "text" or Path(args.config).exists():
            configure(args.config, args.host)
        lock = hold_batch(args.batch) if args.cmd in BATCH_WRITERS and not getattr(args, "dry_run", False) else None  # noqa: F841
        return args.func(args)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
