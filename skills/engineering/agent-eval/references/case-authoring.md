# Authoring Eval Cases

A case is only as good as its evidence. The candidate must face what the original
agent faced — no more (no hints, no later knowledge), no less (facts the agent had
from the conversation that the repository cannot show).

## Finding

- `scripts/mine_corrections.py extract` reads the project's Claude Code transcripts
  (`~/.claude/projects/<encoded-repo-path>*`, worktrees included) and, if used,
  Codex rollouts (`--codex-home ~/.codex --codex-cwd <repo path>`; Codex keeps all
  projects together). It keeps every message the owner typed after an agent turn
  and drops only injected text: tool results, compaction summaries, reminders,
  subagent threads. `--model` targets the model you want to replace, `--since`
  recent work.
- `classify` sends batches to a small model (`--classifier claude:claude-haiku-4-5`
  or `codex:<model>`): the owner's own words or not, correction or not, kind
  (judgment, instruction, fact, style), whether it is about code the agent wrote
  (a lead for an implementation case), and a one-line summary. Thinking stays on:
  without it, recall on known corrections fell from 4/4 to 1/4. About $0.004 per
  message and 20 messages per 1–2 minutes a call; set `--budget` and `--parallel`,
  rerun to resume. Check recall on corrections you already know (the moment each
  case records, not its task messages) before trusting it on the rest, and read a
  sample of both labels.
- `leads` lists the corrections. For each, read the surrounding turns. Keep a lead
  when the correction changes the agent's judgment: wrong root cause, placement,
  stale SOP, skipped verification, waiting instead of acting, scope creep. Drop
  wording fixes.
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
  drop it to raise difficulty. A line holding an earlier owner ruling that the
  Trap penalises following must say the ruling was superseded, or go: otherwise
  the case punishes obeying the owner.
- `owner_messages`: the owner's words, verbatim, in order.
- `attachments`: `{"file", "place"}` puts a file into the snapshot at `place`
  (for an untracked draft the agent was reviewing); `{"file", "inline": true}`
  appends it to the prompt (for a list of tickets). Strip any authoring note from
  an attachment — a header saying "this is what the case tests" is a leak.
- `trap.description`: what the original agent did wrong and why it was wrong.
- `trap.direction`: the minimum that shows the right way (e.g. "recommends
  triggering a real order now instead of waiting"). Judged separately from pass.
  State a property of the answer, not a mechanism, and never qualify it with
  "as the default, not as an option with preconditions": two judges read that two
  ways. `lint.direction_forbidden` in the config lists phrases the lint warns on.
- `trap.pass`: done right — the owner's full standard. Owner review often makes this
  stricter; a strict pass with no direction level gives a Trap nobody passes and no
  signal. Write it as clauses, `[{"text", "checked_by": [ids]}]`, each citing the
  rubric ids or hidden test ids (`classname::name`, as junit reports them) that
  check it; the judges see the clauses numbered. `lint-case` fails a cited id that
  does not exist and warns on a clause citing none or on a single-string pass. A
  clause is a property ("every neighbouring strike within the fallback tolerance is
  verified"), not the reference implementation's arithmetic.
- `trap.wrong_markers` (optional): regular expressions for the wrong approach.
  `lint-case` warns on every attachment or background line that matches: the case
  material itself suggests the wrong approach (an attachment saying "keep the
  machine directories for now" under a Trap that penalises keeping them).
- `rubric`: 4–7 yes/no items, each with `evidence` naming where a judge looks
  (trajectory tool calls, answer text, plan section). Items test path (read X before
  asserting Y) and plan quality (safe ordering, self-collectable acceptance). An item
  that holds for one kind of case only carries `kinds: ["plan"]` or `["implement"]`
  (absent means both): "attempted no side effects" is a planning rule, and an
  implementer following an approved plan changes configuration by design.
- `equivalents`: alternatives that count as right, and near-misses that do not.
- `leak_markers`: strings that would appear only if the answer leaked (the later
  issue number, the eventual feature name). Verify each is absent from the
  snapshot: `git grep -n -E '<m1>|<m2>' <sha>`.
- `calibration_negative`: the original agent's own answer, saved as a file.
- `calibration_positive` (required): the answer the owner accepted after the
  correction, usually the agent's next reply. Without a positive, a Trap nobody can
  pass looks the same as models that all fail, so `calibrate` fails the case and
  `run` refuses it.
- `role` (optional): `"ranking"` (default) or `"baseline"`. A baseline case checks
  that an implementer can finish an approved plan; the report shows it under its own
  heading and it is not used to compare models. Use it when every candidate lands
  in the same narrow band.

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
  `evalkit.py calibrate-tests --case X` must show the bare snapshot failing, the
  reference passing every test, each alternative failing only excused tests and
  `lint-case` finding no error, all inside the sandbox. Record the bare count as
  `baseline`: tests of unchanged behaviour pass on the snapshot, so a score is
  read against it, not against zero.
- `interface`: the names and signatures the hidden tests call. Tests that bind to
  new names cannot be passed without them; say in the review page what this gives
  away (usually the Trap's direction) so the owner can accept it. Include every
  field, key, default and log phrase a test reads that the snapshot lacks.
- Hidden tests assert behaviour, not wording: an error's type, a return value, that
  no further broker call was made. A test asserting a message sentence passes only
  for the reference unless that sentence is in `interface`. A fake standing in for a
  production type (a broker `Contract`) exposes that type's public fields, filled
  consistently, or `interface` says exactly which fields exist: a fake with only
  `conId` punishes code that reads `secType`.
- `evalkit.py lint-case --case X` checks both: every string a hidden test asserts
  (`in`, `==`, `!=`, `pytest.raises(match=)`) must appear in what the candidate
  reads, and every attribute, constant key, keyword argument or imported name a
  test uses must exist in the snapshot or be disclosed. It ignores identifiers,
  numbers, short ASCII tokens and text the tests feed in themselves, and names on
  stdlib, pytest and third-party objects. For a given-plan case it lists the tests
  touching an undisclosed name with their docstrings: read them against the plan,
  since a plan that says the opposite of a test is invisible to both checks. A name
  that exists anywhere in the snapshot counts as present, so a clean name check is
  weaker evidence than a clean literal check.
- `hidden_tests.reference_only` (optional): test ids (`classname::name`) that
  encode the real fix's own design rather than the task. They run and are reported,
  but leave the score and its denominator (`scored_expected`).
- `alt_patches` (required): at least one alternative implementation, usually a
  candidate's diff from an earlier batch that followed another reasonable plan,
  with test files stripped: `[{"file", "note", "accepted_failures": {"<test id>":
  "<reason>"}}]`. `calibrate-tests` runs each; every test the reference passes and
  an alternative fails must be reference-only or listed in its `accepted_failures`
  as a genuine defect of that patch. A failure that is only a design difference
  means the test must change.
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

An implementation case's additions, beside the fields of `assets/case.example.json`:

```json
{
 "kind": "implement",
 "modes": ["given-plan"],
 "role": "baseline",
 "interface": ["`quote_liveness(quote, underlying, *, now, hard_cap_s) -> Liveness`; `Liveness.reason` is one of `REASONS`"],
 "hidden_tests": {
  "files": [{"file": "hidden/test_liveness.py.hidden", "place": "tests/test_liveness.py"}],
  "paths": ["tests/test_liveness.py"],
  "expected": 40,
  "baseline": 12,
  "reference_only": ["tests.test_liveness::test_the_reason_order_matches_the_merged_fix"]
 },
 "reference_patch": "reference.patch",
 "alt_patches": [
  {"file": "alt-sonnet-medium.patch", "note": "a given-plan run from batch i1, tests stripped",
   "accepted_failures": {"tests.test_liveness::test_a_refused_stream_forgets_its_quote": "keeps the last ask after a refusal"}}
 ],
 "trap": {
  "description": "…", "direction": "…",
  "pass": [{"text": "a row without stream metadata is judged dead", "checked_by": ["tests.test_liveness::test_old_rows_fail_closed", "R2"]}]
 }
}
```

## Owner review

Render with `scripts/review_page.py`, publish it, and ask for marks. Each case tab
ends with its case checks (the `lint-case` findings); settle every error before
calibrating. Apply every
"change" and "drop", then re-render so the page shows the final state. Answers to
open questions on the page ("should this hint stay?") are decisions — ask
explicitly when a mark is ambiguous.
