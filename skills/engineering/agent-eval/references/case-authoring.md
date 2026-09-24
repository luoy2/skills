# Authoring Eval Cases

A case is only as good as its evidence. The candidate must face what the original
agent faced — no more (no hints, no later knowledge), no less (facts the agent had
from the conversation that the repository cannot show).

## Finding

- Run `scripts/mine_corrections.py` over the project's Claude Code transcripts
  (`~/.claude/projects/<encoded-repo-path>/*.jsonl`) and, if used, Codex rollouts
  (`--codex-home ~/.codex`). Filter by `--model` to target the model you want to
  replace, `--since` for recent work.
- Keyword hits include pasted handoffs and rhetorical questions. For each hit read
  the surrounding turns. Keep a hit when the correction changes the agent's
  judgment: wrong root cause, placement, stale SOP, skipped verification,
  waiting instead of acting, scope creep. Drop wording fixes and typos.
- One case per correction moment. A long session can yield several.
- Prefer moments whose right answer the owner stated clearly, and whose task can
  be posed as "given this snapshot and instruction, answer and plan".
- Candidates JSON fields for `picker_page.py`: `id, title, task, wrong, fix
  (owner's words), right, tags (error type, offline-verifiable or not), source
  (session + timestamp), snapshot, rec, note`. Recommend a diverse set and say why.

## Evidence

Delegate the transcript reading to a read-only subagent when sessions are large;
ask it for these fields per case and to write them to a file, not into chat:

1. **Instruction, verbatim**, with timestamps. Trace back to where the task began —
   the triggering message is often several turns before the correction.
2. **Facts known at the time**, each with its source, marked whether the snapshot
   can show it. Facts only in the conversation or runtime (counts, machine state,
   earlier rulings, what the agent itself did an hour ago) must go into the
   background.
3. **Trajectory**: what the agent did between instruction and correction, which
   files it read and did not read.
4. **Correction verbatim** and what was eventually decided or built (issue, commit).
5. **Snapshot SHA**: run `scripts/find_snapshot.py --checkout <the checkout the agent
   worked in> --at <correction time>`. The checkout's reflog is the evidence; a
   SessionStart line like `origin/master@<sha>` corroborates. The first-parent
   remote guess is often wrong (two of three cases in the first batch this kit ran).
   Untracked files the agent read (a draft plan) are part of the snapshot: restore
   them from the transcript's Read tool result, strip line-number prefixes, and
   attach them.
6. **Leak sources**: the later issue body, later commits and files, scratch notes,
   handoff files, memory entries (including your own notes about this eval). Check
   whether the snapshot already contains the answer.

## Writing

`case.json` fields (see `assets/case.example.json`):

- `snapshot` (40 hex), `snapshot_evidence`, `source`, `title`.
- `background`: numbered neutral facts. State facts, not judgments; remove
  scheduling opinions and anything that names the fix. A background line such as
  "the agent earlier proposed X" can hint the answer — the owner may choose to
  drop it to raise difficulty.
- `owner_messages`: the owner's words, verbatim, in order.
- `attachments`: `{"file", "place"}` puts a file into the snapshot at `place`
  (for an untracked draft the agent was reviewing); `{"file", "inline": true}`
  appends it to the prompt (for a list of tickets). Strip any authoring note from
  an attachment — a header saying "this is what the case tests" is a leak.
- `trap.description`: what the original agent did wrong and why it was wrong.
- `trap.direction`: the minimum that shows the right way (e.g. "recommends
  triggering a real order now instead of waiting"). Judged separately from pass.
- `trap.pass`: done right — the owner's full standard. Owner review often makes this
  stricter; a strict pass with no direction level gives a Trap nobody passes and no
  signal.
- `rubric`: 4–7 yes/no items, each with `evidence` naming where a judge looks
  (trajectory tool calls, answer text, plan section). Items test path (read X before
  asserting Y) and plan quality (safe ordering, self-collectable acceptance).
- `equivalents`: alternatives that count as right, and near-misses that do not.
- `leak_markers`: strings that would appear only if the answer leaked (the later
  issue number, the eventual feature name). Verify each is absent from the
  snapshot: `git grep -n -E '<m1>|<m2>' <sha>`.
- `calibration_negative`: the original agent's own answer, saved as a file.
- `calibration_positive` (optional, strongly recommended): the answer the owner
  accepted after the correction, usually the agent's next reply. Without a positive,
  a Trap nobody can pass looks the same as models that all fail.

`cases/common.json` holds the answer instructions appended to every prompt
(`suffix`; `implement_suffix`, `planner_suffix` and `split_plan_intro` for
implementation cases), shared rubric items and shared leak markers.

## Implementation cases

Use one when the correction was about code the agent wrote, the fix is merged,
and the fix PR carries offline tests. `"kind": "implement"` adds:

- `snapshot`: the fix merge's parent (check that the touched directories did not
  change between the correction moment and that parent).
- `hidden_tests`: `files` (`{"file", "place"}`, taken from the merge commit),
  `paths` for pytest and `expected`, the total the real fix passes. Place
  every changed test file and support module, not only new ones: a test file the
  fix rewrote still encodes the old behaviour in the snapshot.
- `reference_patch`: `git diff <parent> <merge> -- <source paths>`, without tests.
  `evalkit.py calibrate-tests --case X` must show the bare snapshot failing and
  the reference passing all `expected` inside the sandbox. Record the bare count as
  `baseline`: tests of unchanged behaviour pass on the snapshot, so a score is
  read against it, not against zero.
- `interface`: the names and signatures the hidden tests call. Tests that bind to
  new names cannot be passed without them; say in the review page what this gives
  away (usually the Trap's direction) so the owner can accept it.
- `modes` (optional): narrow the delivery modes. When the hidden tests follow a
  design reached only after review rounds, give the approved plan as an inline
  attachment and use `["given-plan"]`; a candidate cannot guess that design.
- Trap and rubric are judged on the candidate's final message plus its diff.
  Calibration samples are text plus a diff: the negative is the original agent's
  change, the positive the real fix.
- Case material that quotes historical code (hidden tests, reference patch,
  calibration samples) keeps a suffix outside `.py` / `.md` (`.hidden`, `.patch`,
  `.txt`): repository-wide scans for retired names would otherwise fail on a
  faithful copy of the old code (this once turned a regression suite red).
- `ask` (optional): a decision the owner must make on the review page.

## Owner review

Render with `scripts/review_page.py`, publish it, and ask for marks. Apply every
"change" and "drop", then re-render so the page shows the final state. Answers to
open questions on the page ("should this hint stay?") are decisions — ask
explicitly when a mark is ambiguous.
