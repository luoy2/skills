#!/usr/bin/env -S uv run --script
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
macOS sandbox that denies content reads outside the run directory; Claude
candidates run in `--restricted` mode with read-only file tools confined to the
snapshot. All repository and machine specifics come from the config file
(`--config`, see the skill's assets/config.example.json).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import shutil
import signal
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
DENY_ROOTS = ("/Users", "/Volumes", "/private/tmp")
CLAUDE_TOOLS = "Read,Glob,Grep"
IMPL_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
# Paths a sandboxed candidate may read besides its run directory: the shared
# dependency-only test venv, its interpreter and the Claude CLI install.
ALLOW_READ: tuple = ()
TEST_PYTHON = ""
# Environment a candidate must never inherit: forge, vault, cloud and routing credentials.
FORBIDDEN_ENV_PREFIXES = ("GH_", "GITHUB_", "OP_", "AWS_", "SSH_AUTH_SOCK")


def configure(path):
    """Load the eval config; relative paths resolve against the config file's directory."""
    global CFG, REPO, CASES, RESULTS, SCRATCH_ROOT, CLAUDE_BIN, CODEX_BIN, GATEWAY_URL, \
        GATEWAY_TOKEN_COMMAND, GATEWAY_KEY_ENV, INSTRUCTIONS_FILE, DENY_ROOTS, FORBIDDEN_ENV_PREFIXES, \
        ALLOW_READ, TEST_PYTHON, OVERLAY_DIR
    path = Path(path).expanduser().resolve()
    CFG = json.loads(path.read_text(encoding="utf-8"))
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
    DENY_ROOTS = tuple(iso.get("deny_roots", DENY_ROOTS))
    FORBIDDEN_ENV_PREFIXES = tuple(iso.get("forbidden_env_prefixes", FORBIDDEN_ENV_PREFIXES))
    # Both the configured path and its resolved target: a symlinked CLI is checked at each.
    ALLOW_READ = tuple(dict.fromkeys(q for p in iso.get("allow_read", [])
                                     for q in (os.path.expanduser(p), os.path.realpath(os.path.expanduser(p)))))
    TEST_PYTHON = os.path.expanduser((CFG.get("implement") or {}).get("test_python", ""))
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
                                     "candidate": cand, "effort": effort, "mode": mode})
    return runs


def build_prompt(case, common, suffix=None):
    lines = ["Background:"]
    lines += [f"{i}. {item}" for i, item in enumerate(case["background"], 1)]
    for att in case.get("attachments", []):
        if att.get("inline"):
            text = (case["dir"] / att["file"]).read_text(encoding="utf-8")
            lines += ["", f"{att.get('label') or 'Attachment ' + Path(att['file']).name}:", text.strip()]
    lines += ["", "The owner (verbatim, in order):"]
    lines += [f"> {m}" for m in case["owner_messages"]]
    if case.get("interface"):
        lines += ["", "The acceptance tests call the interfaces below; keep their names and signatures, the behaviour is yours to decide:"]
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


def sandbox_profile(root):
    """Deny reading contents of, and writing to, user data, mounts and every other temp tree.

    Metadata stays readable: Codex canonicalizes CODEX_HOME at start, which stats each
    parent under /private/tmp, and denying that aborts it before the first request
    (measured 2026-09-23). Listing a directory or reading a file is content and stays denied.
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
    )


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


def probe_sandbox(profile_path, root, denied_file):
    """The sandbox must refuse a file outside the run root and allow one inside it."""
    inside = Path(root) / "wt" / INSTRUCTIONS_FILE
    failures = []
    outside = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile_path), "/bin/cat", str(denied_file)],
                             capture_output=True)
    if outside.returncode == 0:
        failures.append(f"sandbox allowed reading {denied_file}")
    if inside.exists():
        ok = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile_path), "/bin/cat", str(inside)],
                            capture_output=True)
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


def probe_test_python(profile_path, root):
    """The shared test interpreter must start inside the sandbox, or no hidden test can run."""
    if not TEST_PYTHON:
        return ["implement: config has no implement.test_python"]
    proc = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile_path), TEST_PYTHON, "-c", "import pytest"],
                          capture_output=True, text=True, cwd=root,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(Path(root) / "home"), "TMPDIR": str(Path(root) / "tmp")})
    return [] if proc.returncode == 0 else [f"test python cannot start in the sandbox: {proc.stderr[-300:]}"]


def isolation_check(root, markers, prompt, env, runtime, client, argv=None, profile_path=None, implement=False):
    """Every failure is a reason not to launch. An empty list is the only pass."""
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
        if profile_path is None:
            failures.append(f"{runtime} run has no sandbox profile")
        else:
            failures += probe_sandbox(profile_path, root, REPO / INSTRUCTIONS_FILE)
            if implement:
                failures += probe_test_python(profile_path, root)
        if implement and runtime == "claude" and argv is not None:
            if argv[:2] != ["/usr/bin/sandbox-exec", "-f"]:
                failures.append("write-capable claude is not wrapped in sandbox-exec")
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


def codex_argv(model, effort, profile_path, cwd, resume=None, schema_path=None, out_path=None,
               sandbox="danger-full-access"):
    argv = ["/usr/bin/sandbox-exec", "-f", str(profile_path), CODEX_BIN, "exec"]
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
    return argv + ["-"]


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
            argv, env = claude_impl_argv(cand["model"], effort, ctx["profile"]), impl_env(root, ctx["key"], cand)
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
    argv = codex_argv(cand["model"], effort, ctx["profile"], wt, resume=resume)
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
    prompt = (
        "You review this delivery plan. Below are the task given to a candidate agent and the answer and plan it returned. "
        "You may read the repository. List factual errors, missed risks, what should be verified first and what could be "
        "simpler, one point at a time, each with its evidence. Do not rewrite the plan for it.\n\n=== Task ===\n"
        f"{case_prompt}\n\n=== Candidate's answer and plan ===\n{draft}\n"
    )
    argv = codex_argv(rev["model"], rev["effort"], ctx["profile"], root / "wt")
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


ADOPT_INSTRUCTION = (
    "\n\n=== Previous round ===\nBelow are the answer and plan you returned for this task last round, and a reviewer's comments on them. "
    "Adopt every comment and return the complete revised answer and plan. You may decline a comment only by citing concrete "
    "repository evidence (file and line) that it is wrong, and you must write that evidence down.\n\n"
    "=== Your previous answer and plan ===\n{draft}\n\n=== Review ===\n{review}\n"
)


def adopt_prompt_extra(draft, review):
    """Forced adoption: the author may reject a review point only with repository evidence."""
    return ADOPT_INSTRUCTION.format(draft=draft, review=review)


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
    profile = root / "sandbox.sb"
    profile.write_text(sandbox_profile(root), encoding="utf-8")
    if cand["runtime"] == "codex":
        write_codex_home(root / "codex-home", cand.get("client", "gateway"))
    ctx = {"cand": cand, "effort": effort, "root": root, "out": out, "key": key, "timeout": timeout,
           "profile": profile, "reviewer": arms["reviewer"]}
    markers = case.get("leak_markers", []) + common.get("leak_markers", [])
    if cand["runtime"] == "claude":
        env = claude_env(cand["client"], key)
        argv = claude_argv(cand["model"], effort, append_file=instructions(root / "wt"))
    else:
        env = codex_env(root, root / "codex-home", key)
        argv = None
    base = {"run_id": run["run_id"], "batch": batch, "source_run": run.get("source_run"), "case": case["id"], "snapshot": case["snapshot"],
            "overlay": overlay_files(), "candidate": cand["id"], "model": cand["model"], "runtime": cand["runtime"],
            "client": cand["client"], "effort": effort, "mode": mode, "started_at": now()}
    failures = isolation_check(root, markers, prompt, env, cand["runtime"], cand["client"],
                               argv=argv, profile_path=profile)
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
            revise_prompt = ("A reviewer commented on your answer and plan as below. Revise accordingly and return the complete "
                             "revised answer and plan; explain any comment you disagree with.\n\n=== Review ===\n" + review["final"])
            revised, sid2 = run_candidate_stage(ctx, "revise", revise_prompt, resume=sid)
            stages.append(revised)
            sessions.append(sid2)
    usage, models, efforts, steps, found = collect_usage(ctx, sessions)
    (out / "trajectory.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1), encoding="utf-8")
    # Each snapshot is a full checkout (~340 MB); 109 of them filled the disk on 2026-09-23.
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


def load_records(batch, name="runs.jsonl"):
    latest = {}
    for row in read_jsonl(batch_dir(batch) / name):
        latest[row.get("key") or row["run_id"]] = row
    return latest


class RecordWriter:
    """The single writer of a batch ledger: one appended JSON line per terminal state."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def append(self, row):
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self.lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def cmd_run(args):
    common, cases = load_cases(args.cases)
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
        futures = {pool.submit(execute_run, r, args.batch, common, cases, arms, prices, key, args.timeout): r
                   for r in todo}
        for fut in concurrent.futures.as_completed(futures):
            run = futures[fut]
            try:
                record = fut.result()
            except Exception as exc:  # a crashed run is recorded, never silently dropped
                record = {"run_id": run["run_id"], "batch": args.batch, "case": run["case"],
                          "candidate": run["candidate"]["id"], "model": run["candidate"]["model"],
                          "effort": run["effort"], "mode": run["mode"], "status": "invalid",
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
    """
    arms, prices = load_arms(), load_prices()
    cands = {c["id"]: c for c in arms["candidates"]}
    writer = RecordWriter(batch_dir(args.batch) / "runs.jsonl")
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
    """One forced-adoption run per earlier review run and repeat, reusing its draft and review."""
    runs = []
    for rec in sorted(source_records, key=lambda r: r["run_id"]):
        if rec.get("mode") != "review" or rec.get("status") != "valid":
            continue
        if candidates and rec["candidate"] not in candidates or efforts and rec["effort"] not in efforts:
            continue
        review = next((s.get("final") or "" for s in rec["stages"] if s["stage"] == "review"), "")
        if not review.strip() or not (rec.get("draft") or "").strip():
            continue
        for rep in range(1, repeats + 1):
            runs.append({"run_id": f"{rec['case']}-{rec['candidate']}-{rec['effort']}-adopt-r{rep}",
                         "candidate_id": rec["candidate"], "case": rec["case"], "effort": rec["effort"], "mode": "adopt",
                         "source_run": rec["run_id"], "prompt_extra": adopt_prompt_extra(rec["draft"], review)})
    return runs


def cmd_adopt(args):
    common, cases = load_cases(args.cases)
    arms, prices = load_arms(), load_prices()
    cands = {c["id"]: c for c in arms["candidates"]}
    source = [r for r in load_records(args.from_batch).values() if r["case"] in cases]
    runs = adopt_runs(source, args.candidates, args.efforts, args.repeats)
    for r in runs:
        r["candidate"] = cands[r["candidate_id"]]
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
        futures = {pool.submit(execute_run, r, args.batch, common, cases, arms, prices, key, args.timeout): r
                   for r in todo}
        for fut in concurrent.futures.as_completed(futures):
            run = futures[fut]
            try:
                record = fut.result()
            except Exception as exc:
                record = {"run_id": run["run_id"], "batch": args.batch, "case": run["case"],
                          "candidate": run["candidate"]["id"], "model": run["candidate"]["model"],
                          "effort": run["effort"], "mode": "adopt", "source_run": run["source_run"],
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
    profile = root / "sandbox.sb"
    profile.write_text(sandbox_profile(root), encoding="utf-8")
    markers = case.get("leak_markers", []) + common.get("leak_markers", [])
    if args.plant:
        (root / "home" / "notes.md").write_text(f"answer: {markers[0]}\n", encoding="utf-8")
    prompt = build_prompt(case, common)
    env = codex_env(root, root / "codex-home", "placeholder")
    failures = isolation_check(root, markers, prompt, env, "codex", "gateway", profile_path=profile,
                               implement=is_impl(case))
    print(json.dumps({"case": args.case, "planted": args.plant, "pass": not failures, "failures": failures},
                     ensure_ascii=False, indent=1))
    return 0 if not failures else 1


# ------------------------------------------------------------ implementation

def is_impl(case):
    return case.get("kind") == "implement"


def impl_cfg():
    impl = CFG.get("implement")
    if not impl:
        raise EvalError("config has no 'implement' section")
    return impl


def claude_impl_argv(model, effort, profile):
    """Write-capable Claude: the whole CLI runs inside the run's sandbox profile.

    Tool permissions are skipped because the sandbox, not the prompt, is the boundary:
    reads and writes outside the run directory fail at the kernel.
    """
    return ["/usr/bin/sandbox-exec", "-f", str(profile), str(CLAUDE_BIN), "-p", "--model", model,
            "--effort", effort, "--tools", IMPL_TOOLS, "--dangerously-skip-permissions",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format", "stream-json", "--verbose"]


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
                                     "candidate": cand, "effort": effort, "mode": mode, "rep": rep})
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
                         "effort": p["effort"], "mode": "solo", "suffix": common["planner_suffix"]})
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


def parse_junit(path, expected):
    import xml.etree.ElementTree as ET
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    try:
        tree = ET.parse(path)
    except (FileNotFoundError, ET.ParseError):
        return {**counts, "expected": expected, "pass_rate": 0.0, "junit": False}
    for tc in tree.iter("testcase"):
        tags = {child.tag for child in tc}
        key = ("failed" if "failure" in tags else "errors" if "error" in tags
               else "skipped" if "skipped" in tags else "passed")
        counts[key] += 1
    return {**counts, "expected": expected, "pass_rate": round(counts["passed"] / expected, 4) if expected else None,
            "junit": True}


def run_hidden_tests(case, root, profile, out, timeout):
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
    argv = ["/usr/bin/sandbox-exec", "-f", str(profile), TEST_PYTHON, "-m", "pytest", *ht["paths"],
            "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", f"--junitxml={junit}"]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path(root) / "home"), "TMPDIR": str(Path(root) / "tmp"),
           "LANG": "en_US.UTF-8", "PYTHONPATH": str(wt), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = run_process(argv, "", wt, env, timeout, out / "hidden-tests.stdout.txt", out / "hidden-tests.stderr.txt")
    if junit.exists():
        shutil.copyfile(junit, out / "hidden-junit.xml")
    return {"returncode": proc["returncode"], "elapsed_s": proc["elapsed_s"], "timed_out": proc["timed_out"],
            **parse_junit(junit, ht["expected"])}


def execute_impl_run(run, batch, common, cases, impl, prices, key, timeout, plans):
    case = cases[run["case"]]
    cand, effort, mode = run["candidate"], run["effort"], run["mode"]
    root = run_dir(batch, run["run_id"])
    out = batch_dir(batch) / "runs" / run["run_id"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    base = {"run_id": run["run_id"], "batch": batch, "kind": "implement", "case": case["id"],
            "snapshot": case["snapshot"], "overlay": overlay_files(), "candidate": cand["id"], "model": cand["model"],
            "runtime": cand["runtime"],
            "client": cand["client"], "effort": effort, "mode": mode, "rep": run["rep"], "started_at": now()}
    prompt = build_prompt(case, common)
    plan = None
    if planner_of(mode):
        plan = plans.get(plan_run_id(case["id"], planner_of(mode), run["rep"]))
        if not plan or plan.get("status") != "valid":
            return {**base, "status": "invalid", "reasons": ["plan unavailable"], "stages": [], "finished_at": now()}
        prompt += "\n\n" + common["split_plan_intro"] + "\n\n=== Plan ===\n" + plan["final"]
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    wt = prepare_snapshot(case, root)
    (root / "claude-config").mkdir()
    base_sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    profile = root / "sandbox.sb"
    profile.write_text(sandbox_profile(root), encoding="utf-8")
    if cand["runtime"] == "codex":
        write_codex_home(root / "codex-home", cand.get("client", "gateway"))
    ctx = {"cand": cand, "effort": effort, "root": root, "out": out, "key": key, "timeout": timeout,
           "profile": profile, "implement": True}
    markers = case.get("leak_markers", []) + common.get("leak_markers", [])
    argv = claude_impl_argv(cand["model"], effort, profile) if cand["runtime"] == "claude" else None
    failures = isolation_check(root, markers, prompt, impl_env(root, key, cand), cand["runtime"], cand["client"],
                               argv=argv, profile_path=profile, implement=True)
    if failures:
        shutil.rmtree(root / "wt", ignore_errors=True)
        return {**base, "status": "invalid", "reasons": ["isolation: " + f for f in failures],
                "stages": [], "finished_at": now()}
    stage, sid = run_candidate_stage(ctx, "implement", prompt)
    usage, models, efforts, steps, found = collect_usage(ctx, [sid])
    (out / "trajectory.json").write_text(json.dumps(steps, ensure_ascii=False, indent=1), encoding="utf-8")
    diff = capture_diff(wt, base_sha, out)
    tests = run_hidden_tests(case, root, profile, out, impl.get("test_timeout", 900))
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
                          "candidate": run["candidate"]["id"], "model": run["candidate"]["model"],
                          "effort": run["effort"], "mode": run["mode"], "status": "invalid",
                          "reasons": [f"runner crashed: {type(exc).__name__}: {exc}"], "finished_at": now()}
            writer.append(record)
            cost = record.get("chain_cost_usd")
            spent += (record.get("cost_usd") or 0.0)
            tests = record.get("tests")
            tail = f" tests {tests['passed']}/{tests['expected']}" if tests else ""
            print(f"{record['status']:7} {record['run_id']:40} ${cost if cost is not None else '?'} "
                  f"{record.get('elapsed_s', '?')}s{tail} {'; '.join(record.get('reasons', []))[:160]}", flush=True)
            if budget is not None and spent >= budget:
                cancelled = sum(f.cancel() for f in futures)
                print(f"budget ${budget} reached (${spent:.2f}); {cancelled} queued runs not started", flush=True)
                budget = None


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
    plans = ensure_plans(args.batch, todo, common, cases, impl, prices, key, args.timeout, args.parallel)
    _run_pool(todo, lambda r: execute_impl_run(r, args.batch, common, cases, impl, prices, key, args.timeout, plans),
              RecordWriter(batch_dir(args.batch) / "runs.jsonl"), args.batch, args.parallel, args.budget)
    return 0


def cmd_calibrate_tests(args):
    """Hidden tests must fail on the bare snapshot and pass in full once the real fix is applied."""
    common, cases = load_cases([args.case])
    case = cases[args.case]
    if not is_impl(case):
        raise EvalError(f"{args.case} is not an implementation case")
    root = SCRATCH_ROOT / "_calibrate_tests" / args.case
    out = root / "out"
    results = {}
    for arm in ("bare", "reference"):
        wt = prepare_snapshot(case, root)
        out.mkdir(parents=True, exist_ok=True)
        if arm == "reference":
            subprocess.run(["git", "-C", str(wt), "apply", str(case["dir"] / case["reference_patch"])], check=True)
        profile = root / "sandbox.sb"
        profile.write_text(sandbox_profile(root), encoding="utf-8")
        results[arm] = run_hidden_tests(case, root, profile, out, impl_cfg().get("test_timeout", 900))
    if "baseline" in case["hidden_tests"] and results["bare"]["passed"] != case["hidden_tests"]["baseline"]:
        print(f"note: bare snapshot passed {results['bare']['passed']}, case records baseline "
              f"{case['hidden_tests']['baseline']}", file=sys.stderr)
    ok = (results["bare"]["passed"] < case["hidden_tests"]["expected"]
          and results["reference"]["passed"] == case["hidden_tests"]["expected"])
    shutil.rmtree(root, ignore_errors=True)
    print(json.dumps({"case": args.case, "pass": ok, **results}, ensure_ascii=False, indent=1))
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
    note = f"\n(diff omitted for length: {', '.join(omitted)})" if omitted else ""
    return (f"{rec.get('final', '')}\n\n=== Candidate's changes (git diff against the snapshot, "
            f"{rec['diff']['files']} files, +{rec['diff']['added']}/-{rec['diff']['deleted']}) ===\n"
            + ("".join(kept) or "(no changes)") + note)


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


def judge_prompt(case, common, prompt, steps, answer):
    rubric = case["rubric"] + common["rubric"]
    traj = "\n".join(
        f"[{i}] {s['kind']}: {s.get('name', '')} {s.get('input', '')}{s.get('text', '')}".strip()
        for i, s in enumerate(steps, 1)) or "(no tool calls recorded)"
    items = "\n".join(f"- {r['id']}: {r['text']} (evidence: {r['evidence']})" for r in rubric)
    return (
        "You are an eval judge. Below are an eval case, an anonymous candidate agent's tool-call trajectory and its final answer. "
        "Judge only against the given pass conditions and rubric, item by item; do not grade style. Every verdict cites a "
        "trajectory step number or the answer's own words as evidence; no evidence means fail. Equivalent approaches are "
        "accepted as listed under 'Accepted equivalents'.\n\n"
        f"=== Case as the candidate saw it ===\n{prompt}\n\n"
        f"=== Trap (judged at two levels) ===\nThe original agent's mistake: {case['trap']['description']}\n"
        f"direction (right way; only the direction of the recommended approach, details need not be complete): {case['trap']['direction']}\n"
        f"pass (done right): {case['trap']['pass']}\n"
        "If pass is true, direction is true.\n\n"
        f"=== Rubric (judge each) ===\n{items}\n\n=== Accepted equivalents ===\n{case['equivalents']}\n\n"
        f"=== Candidate trajectory ===\n{traj}\n\n=== Candidate final answer ===\n{answer}\n\n"
        f"Output JSON: one trap entry (direction, pass, evidence) and one items entry per rubric id ({', '.join(r['id'] for r in rubric)})."
    )


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
    profile = workdir / "sandbox.sb"
    profile.write_text(sandbox_profile(workdir), encoding="utf-8")
    argv = codex_argv(judge["model"], judge["effort"], profile, workdir, schema_path=schema_path,
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
    The optional positive is the answer the owner accepted after the correction;
    passing it shows the Trap is passable as written. A judge that fails the positive
    is too strict, or the pass condition asks for more than the owner did.
    """
    common, cases = load_cases(args.cases)
    arms, prices = load_arms(), load_prices()
    key = gateway_key()
    writer = RecordWriter(batch_dir(args.batch) / "calibration.jsonl")
    ok = True
    for case in cases.values():
        prompt = build_prompt(case, common)
        samples = [("negative", case["calibration_negative"], False)]
        if case.get("calibration_positive"):
            samples.append(("positive", case["calibration_positive"], True))
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
                ok = ok and good
                writer.append({"key": f"{case['id']}|{kind}|{judge['id']}", "case": case["id"], "judge": judge["id"],
                               "kind": kind, "expected": expect, "verdict": verdict,
                               "cost_usd": cost_usd(usage, prices), "elapsed_s": elapsed, "at": now()})
                print(f"{case['id']} {kind:8} {judge['id']:12} direction={levels[0]} pass={levels[1]} "
                      f"{'OK' if good else 'CALIBRATION FAIL'}")
    return 0 if ok else 1


# -------------------------------------------------------------------- report

def summarize(batch, cases, arms):
    records = load_records(batch)
    judgments = load_records(batch, "judgments.jsonl")
    rows = []
    for rec in records.values():
        row = {k: rec.get(k) for k in ("run_id", "case", "candidate", "model", "effort", "mode", "status",
                                        "reasons", "cost_usd", "chain_cost_usd", "elapsed_s", "kind", "rep",
                                        "tests", "diff", "plan_run", "plan_cost_usd")}
        row["scores"] = {}
        targets = [("final", None)] if rec.get("kind") == "implement" else judge_targets(rec)
        for target, _ in targets if rec.get("status") == "valid" else []:
            per_judge = {}
            for judge in arms["judges"]:
                j = judgments.get(f"{rec['run_id']}|{target}|{judge['id']}")
                if j and "verdict" in j:
                    v = j["verdict"]
                    per_judge[judge["id"]] = {"trap": v["trap"]["pass"], "direction": v["trap"].get("direction"),
                                              "items": {i["id"]: i["pass"] for i in v["items"]},
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
    rows = summarize(args.batch, cases, arms)
    out = Path(args.out) if args.out else batch_dir(args.batch) / "report.html"
    out.write_text(render_report(args.batch, rows, cases, arms), encoding="utf-8")
    (batch_dir(args.batch) / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                                        encoding="utf-8")
    print(out)
    if args.md:
        Path(args.md).write_text(render_markdown(args.batch, rows, cases, arms), encoding="utf-8")
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


def render_markdown(batch, rows, cases, arms):
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
    plans = {r["plan_run"]: r.get("plan_cost_usd") or 0 for r in valid if r.get("plan_run")}
    run_cost = (sum(r.get("chain_cost_usd") or 0 for r in valid if not r.get("plan_run"))
                + sum(r.get("cost_usd") or 0 for r in valid if r.get("plan_run")) + sum(plans.values()))
    judge_cost = sum(j.get("cost_usd") or 0 for j in judgments.values())
    out = [f"## Data: batch `{batch}`", "",
           f"{len(valid)} valid runs, {len(invalid)} invalid; run cost ${run_cost:.2f} (reviewer included; a shared plan counted once), "
           f"judging cost ${judge_cost:.2f}. Trap is shown as direction/pass: ✓ both judges pass, ✗ both fail, ? they disagree or "
           "did not judge; rubric is the number of items both judges pass / total.", ""]
    overlaid = sorted({f for r in valid for f in (r.get("overlay") or {})})
    if overlaid:
        out += ["Snapshot code unchanged; these instruction files were replaced by the overlay_dir versions: "
                + ", ".join(f"`{f}`" for f in overlaid) + ".", ""]
    disputes = []
    for case_id, case in cases.items():
        case_rows = [r for r in valid if r["case"] == case_id]
        if not case_rows:
            continue
        out += [f"### {case_id} · {case['title']}", ""]
        if is_impl(case):
            ht = case["hidden_tests"]
            out += [f"{ht['expected']} hidden tests; the bare snapshot passes {ht.get('baseline', '?')}.", "",
                    "| Model | effort | Delivery | Hidden tests (per run) | Trap direction/pass | Rubric | Mean cost | Mean time |",
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
                    _, agreed, disputed = agreement(per) if per else (None, None, [])
                    items.append(f"{agreed[0]}/{agreed[1]}" if agreed else "—")
                    if per and (d is None or t is None):
                        disputes.append(f"{r['run_id']}: trap")
                costs = [r["chain_cost_usd"] for r in reps if r.get("chain_cost_usd") is not None]
                secs = [r["elapsed_s"] for r in reps if r.get("elapsed_s")]
                out.append(f"| {key[0]} | {key[1]} | {key[2]} | {' · '.join(str(r['tests']['passed']) for r in reps)} | "
                           f"{' · '.join(traps)} | {' · '.join(items)} | "
                           f"{'$%.2f' % (sum(costs) / len(costs)) if costs else '?'} | "
                           f"{int(sum(secs) / len(secs)) if secs else '?'}s |")
        else:
            out += ["| Model | effort | Delivery | Trap (draft) | Rubric (draft) | Trap (final) | Rubric (final) | Chain cost | Time |",
                    "|---|---|---|---|---|---|---|---|---|"]
            for r in sorted(case_rows, key=lambda r: (r["model"], effort_order.index(r["effort"]), r["mode"])):
                cells = []
                for target in ("draft", "final"):
                    per = r["scores"].get(target) or (r["scores"].get("final") if r["mode"] != "review" else {}) or {}
                    d, t = _verdict(per)
                    _, agreed, _ = agreement(per) if per else (None, None, [])
                    cells += [f"{mark[d]}/{mark[t]}", f"{agreed[0]}/{agreed[1]}" if agreed else "—"]
                    if per and t is None:
                        disputes.append(f"{r['run_id']} {target}: trap")
                if r["mode"] != "review":
                    cells[2:4] = ["= draft", "= draft"]
                cost = r.get("chain_cost_usd")
                out.append(f"| {r['model']} | {r['effort']} | {r['mode']} | {' | '.join(cells)} | "
                           f"{'$%.2f' % cost if cost is not None else '?'} | {r.get('elapsed_s') or '?'}s |")
        out.append("")
    out += ["### Judge disagreements", ""] + ([f"- {d}" for d in disputes] or ["- none"]) + [""]
    out += ["### Invalid runs", ""] + ([f"- `{r['run_id']}`: {'; '.join(r.get('reasons') or [])}" for r in invalid]
                                    or ["- none"]) + [""]
    return "\n".join(out)


def render_impl_case(case_id, case, rows):
    """One table per implementation case: each arm's repeats side by side, then their mean."""
    esc = html.escape
    effort_order = ["medium", "high", "xhigh", "max", "ultra"]
    groups = {}
    for r in rows:
        groups.setdefault((r["candidate"], r["model"], r["effort"], r["mode"]), []).append(r)
    disputes = []
    out = [f"<h2>{esc(case_id)} · {esc(case['title'])}</h2>"
           f"<p class='lede'>{case['hidden_tests']['expected']} hidden tests; the bare snapshot passes "
           f"{case['hidden_tests'].get('baseline', '?')} (the floor). Trap direction/pass and rubric count only when both judges pass. "
           "Cost includes the full shared plan (split modes).</p><div class='scroll'><table><thead><tr>"
           "<th>Model</th><th>effort</th><th>Delivery</th><th>Hidden tests (per run)</th><th>Mean pass rate</th>"
           "<th>Trap direction/pass</th><th>Rubric</th><th>Diff +/-</th><th>Mean cost</th><th>Mean time</th></tr></thead><tbody>"]
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
            mark = {True: "✓", False: "✗", None: "?"}
            traps.append(f"{mark[direction]}/{mark[trap]}")
            items_txt.append(f"{items[0]}/{items[1]}" if items else "—")
            if disputed:
                disputes.append(f"{r['run_id']}: {', '.join(disputed)}")
            if per and (trap is None or direction is None):
                disputes.append(f"{r['run_id']}: trap")
        costs = [r["chain_cost_usd"] for r in reps if r.get("chain_cost_usd") is not None]
        secs = [r["elapsed_s"] for r in reps if r.get("elapsed_s")]
        diff_txt = " · ".join(f"+{r['diff']['added']}/-{r['diff']['deleted']}" for r in reps)
        out.append(f"<tr><td>{esc(key[1])}</td><td>{esc(key[2])}</td><td>{esc(key[3])}</td>"
                   f"<td class='num'>{tests_txt}</td><td class='num'>{sum(rates) / len(rates):.0%}</td>"
                   f"<td>{' · '.join(traps)}</td><td>{' · '.join(items_txt)}</td><td class='num'>{diff_txt}</td>"
                   f"<td class='num'>{'$%.2f' % (sum(costs) / len(costs)) if costs else '?'}</td>"
                   f"<td class='num'>{int(sum(secs) / len(secs)) if secs else '?'}s</td></tr>")
    out.append("</tbody></table></div>")
    return "".join(out), disputes


def render_report(batch, rows, cases, arms):
    esc = html.escape
    valid = [r for r in rows if r["status"] == "valid"]
    invalid = [r for r in rows if r["status"] != "valid"]
    effort_order = ["medium", "high", "xhigh", "max", "ultra"]
    body = []
    disputes = []
    for case_id, case in cases.items():
        if is_impl(case):
            html_part, case_disputes = render_impl_case(case_id, case, [r for r in valid if r["case"] == case_id])
            body.append(html_part)
            disputes += case_disputes
            continue
        body.append(f"<h2>{esc(case_id)} · {esc(case['title'])}</h2><div class='scroll'><table><thead><tr>"
                    "<th>Model</th><th>effort</th><th>Delivery</th><th>Trap (draft)</th><th>Rubric (draft)</th>"
                    "<th>Trap (final)</th><th>Rubric (final)</th><th>Chain cost</th><th>Time</th></tr></thead><tbody>")
        case_rows = sorted([r for r in valid if r["case"] == case_id],
                           key=lambda r: (r["candidate"], effort_order.index(r["effort"]), r["mode"]))
        for r in case_rows:
            cells = []
            for target in ("draft", "final"):
                per = r["scores"].get(target) or ({} if target == "draft" else {})
                if target == "draft" and r["mode"] == "solo":
                    per = r["scores"].get("final", {})
                trap, items, disputed = agreement(per) if per else (None, None, [])
                trap_txt = {True: "<span class='ok'>pass</span>", False: "<span class='bad'>fail</span>",
                            None: "<span class='warn'>split/unjudged</span>"}[trap]
                item_txt = f"{items[0]}/{items[1]}" if items else "—"
                if disputed:
                    item_txt += f" <span class='warn'>split {','.join(disputed)}</span>"
                    disputes.append(f"{r['run_id']} {target}: {', '.join(disputed)}")
                if trap is None and per:
                    disputes.append(f"{r['run_id']} {target}: trap")
                cells += [trap_txt, item_txt]
            if r["mode"] == "solo":
                cells[2:4] = ["= draft", "= draft"]
            cost = r.get("chain_cost_usd")
            body.append(f"<tr><td>{esc(r['model'])}</td><td>{esc(r['effort'])}</td><td>{esc(r['mode'])}</td>"
                        + "".join(f"<td>{c}</td>" for c in cells)
                        + f"<td class='num'>{'$%.2f' % cost if cost is not None else '?'}</td>"
                        f"<td class='num'>{r.get('elapsed_s') or '?'}s</td></tr>")
        body.append("</tbody></table></div>")
    inv = "".join(f"<li><code>{esc(r['run_id'])}</code>：{esc('; '.join(r.get('reasons') or []))}</li>"
                  for r in invalid) or "<li>none</li>"
    dis = "".join(f"<li>{esc(d)}</li>" for d in disputes) or "<li>none</li>"
    return f"""<title>Agent eval report {esc(batch)}</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1b2130;--muted:#5d6577;--line:#dfe2e8;--ok:#1f7a4d;--bad:#a3303a;--warn:#9a5b00;--card:#fff}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#12151c;--fg:#e6e8ee;--muted:#9aa2b3;--line:#2c3342;--ok:#6cc99a;--bad:#ec8b93;--warn:#e0b060;--card:#1a1f29}}}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#12151c;--fg:#e6e8ee;--muted:#9aa2b3;--line:#2c3342;--ok:#6cc99a;--bad:#ec8b93;--warn:#e0b060;--card:#1a1f29}}
body{{background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;line-height:1.6}}
.wrap{{max-width:1100px;margin:0 auto;padding-inline:16px;padding-block:24px}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:var(--card);font-size:14px}}
th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}}
.num{{font-variant-numeric:tabular-nums;text-align:right}} .ok{{color:var(--ok)}} .bad{{color:var(--bad)}} .warn{{color:var(--warn)}}
.lede{{color:var(--muted);max-width:75ch}}
</style>
<div class="wrap">
<h1>Agent eval report · {esc(batch)}</h1>
<p class="lede">Reference results; nothing here changes your agent configuration. Trap and rubric count only when both judges pass;
disagreements are listed for the owner to rule on. Cost is at list prices over the whole chain including reviewer and revision;
judging is not included. {len(valid)} valid runs, {len(invalid)} invalid.</p>
{''.join(body)}
<h2>Disagreements for the owner to rule on</h2><ul>{dis}</ul>
<h2>Invalid runs</h2><ul>{inv}</ul>
</div>"""


# ---------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.environ.get("AGENT_EVAL_CONFIG", "agent-eval/config.json"),
                    help="eval config (default: $AGENT_EVAL_CONFIG or agent-eval/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    csv = lambda s: [x for x in s.split(",") if x]  # noqa: E731

    run = sub.add_parser("run", help="run candidates and record each run's terminal state")
    run.add_argument("--batch", required=True)
    run.add_argument("--cases", type=csv)
    run.add_argument("--candidates", type=csv)
    run.add_argument("--efforts", type=csv)
    run.add_argument("--modes", type=csv)
    run.add_argument("--parallel", type=int, default=4)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run.add_argument("--rerun", action="store_true", help="rerun runs that are already valid")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--repeats", type=int, help="repeats per arm (implementation default implement.repeats; planning default one run, ids without -rN)")
    run.add_argument("--budget", type=float, help="implementation cases: stop launching once this many dollars are spent")
    run.set_defaults(func=cmd_run)

    ct = sub.add_parser("calibrate-tests", help="implementation case: hidden tests fail bare, pass with the real fix")
    ct.add_argument("--case", required=True)
    ct.set_defaults(func=cmd_calibrate_tests)

    ado = sub.add_parser("adopt", help="forced adoption: rerun authors on earlier drafts and reviews")
    ado.add_argument("--batch", required=True)
    ado.add_argument("--from-batch", required=True)
    ado.add_argument("--cases", type=csv)
    ado.add_argument("--candidates", type=csv)
    ado.add_argument("--efforts", type=csv)
    ado.add_argument("--repeats", type=int, default=1)
    ado.add_argument("--parallel", type=int, default=4)
    ado.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
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

    args = ap.parse_args(argv)
    try:
        configure(args.config)
        return args.func(args)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
