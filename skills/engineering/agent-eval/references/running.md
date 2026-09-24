# Running an eval

## Scope

Settle with the owner: the decision the results feed; candidates (model ids,
runtime `claude`|`codex`, client `native`|`gateway`, effort ladder); delivery
modes; repeats; a budget stop. Look up list prices (never from memory) and record
their source in the config's `prices_note`. Check each candidate is actually
served: a gateway may list `claude-fable-5` but not `claude-fable-5-1`, or put a
model in rate-limit cooldown — probe each with a one-line prompt before a batch.

Delivery modes:
- `solo`: the candidate answers and plans alone.
- `review`: draft → fixed reviewer → the candidate revises in the same session.
  Draft and final are scored separately; cost includes the reviewer.
- `adopt`: reuse a `review` batch's drafts and reviews; a fresh candidate session
  must adopt every review point unless it cites repository evidence against it.
  Cheap (no reviewer call) and separates "the reviewer found it" from "the author
  accepted it".

Implementation cases (config `implement`): candidates and efforts are listed
separately from the planning candidates. Modes are `direct` (the implementer
plans for itself), `split-<planner id>` (a planner from `implement.planners`
writes one read-only plan per case and repeat, shared by every implementer) and
`given-plan` (the case carries the approved plan). `repeats` defaults to
`implement.repeats`; `--budget` stops launching new runs once spent. Cost of a split
run includes its plan in full, since each real delivery pays for its plan.

Measuring an instruction change (a new AGENTS.md or SOP): keep cases and snapshots
fixed and set `overlay_dir` to a folder that mirrors repository paths. Its files
replace or join every snapshot before the snapshot commit, so an implementer's diff
never shows them, and each run records their hashes. Leak markers still apply: drop
from the overlay whatever names a later fix (ticket numbers, flags or files the fix
added) and list each such edit in the report. Planning runs take `--repeats` too
(ids gain `-rN`); compare with the same arm's earlier runs. A rule written after
these very corrections is in-sample: a pass shows the written rule gets applied,
not that it generalizes. Say so next to the result.

## Isolation

Why: a candidate that can read the owner's memory, a later commit or another run's
output is not measuring the model.

- Snapshot: `git archive <sha>` into a fresh one-commit repository, so history after
  the snapshot is invisible. Placed attachments are added untracked.
- Codex candidates run under `sandbox-exec` with `file-read-data` and writes
  denied under the configured roots except the run directory. Deny content reads,
  not all reads: Codex canonicalizes CODEX_HOME at start and aborts if it cannot
  stat parent directories. HOME, TMPDIR and CODEX_HOME live in the run directory;
  the only credential is the gateway key (or a copied native `auth.json`).
- Claude candidates run with `--restricted --tools Read,Glob,Grep
  --strict-mcp-config`: no user settings, global CLAUDE.md or memory, file tools
  confined to the snapshot. `--restricted` also skips project instruction
  discovery, so the snapshot's instructions file is passed explicitly.
- Pre-launch check (every run): no forge/vault/cloud/routing credentials in the
  environment, no leak marker in the run directory or prompt, and for Codex a real
  sandbox probe (a file outside must be unreadable, the snapshot readable). Any
  failure records the run invalid without launching. `check-isolation --plant`
  must fail — that is the control proving the check can fail.
- Implementation runs need write tools, so both runtimes run inside the sandbox:
  Claude with `--tools Read,Glob,Grep,Edit,Write,Bash --dangerously-skip-permissions`,
  its own `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_TMPDIR` in the run directory, and
  therefore only through the gateway. `isolation.allow_read` opens the Claude
  install and a dependency-only test venv (`implement.test_python`); never an
  editable install of the repository, which would expose the real tree. The
  pre-launch check also starts the test interpreter inside the sandbox.
- Hidden tests are copied in only after the candidate stops, then run in the same
  sandbox with `PYTHONPATH` set to the snapshot. The diff is captured first.
- Judges see the prompt, trajectory and answer, never the model name.

## Running

- Smoke first: one cheap effort per client path (native Claude, gateway Claude,
  Codex) in both `solo` and `review`, then `judge` and `report`. This catches
  launch failures before they cost a matrix.
- `run` records one terminal line per run in `runs.jsonl` and skips runs already
  valid, so rerunning the same command resumes. A run is valid only when the
  client's own records prove the requested model and effort ran, every stage
  finished, the answer is non-empty, usage is present and priced.
- Cost and disk: each snapshot is a full checkout and each fresh CODEX_HOME
  unpacks ~90 MB; the runner deletes both when a run ends. Watch free disk anyway.
- Monitor long batches on a fixed cadence (a session cron works): valid/invalid
  counts from the ledger, spend, longest candidate process vs the timeout, output
  freshness per in-flight run, runner alive, free disk. Do not kill processes on
  suspicion; report the evidence.
- Codex candidates may spawn subagents; each writes its own rollout. The runner
  bills every rollout and judges effort by the main thread only. After any change
  to usage accounting, `recompute --batch <b>` re-derives cost and validity from
  the saved rollouts.
- Quota or crashes mid-batch: rerun `run` (valid runs are skipped), then
  `recompute`, `judge` (skips done judgments), `report`.
