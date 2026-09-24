---
name: agent-eval
description: "Build and run an evaluation of agent models and thinking effort from real moments where the owner corrected an agent: mine corrections from Claude Code / Codex session logs, turn each into an isolated Eval Case with a two-level Trap and rubric, have the owner review cases on an annotatable page, run candidates in a sandbox, score with two calibrated judges and report cost-aware reference results. Use this whenever someone wants to compare models or effort levels on their own repository, asks which model or effort a role should use, wants to test whether review/SOP steps help, adds eval cases or research questions to an agent eval set, or asks to rerun the eval on a new model — even if they just say 'benchmark our agents' or 'test the models on our own mistakes'."
---

# Agent eval

Measure which model, thinking effort and delivery mode actually avoid the mistakes
this team's agents made before. Every case is one real moment where the owner
corrected an agent; the candidate gets the same instruction, the same facts and
the same repository snapshot, and is scored on whether it avoids that mistake.
Results are reference evidence for the owner. Nothing here changes role bindings.

The pipeline has one runner (`scripts/evalkit.py`) driven by a per-repository
config, plus page generators for the two owner decisions (which cases, and
whether each case is right). Cases, config and results live in your repository;
`assets/` holds an example of each.

## Workflow

Work through these in order. Each step names the reference to read before doing
it; read only that one.

1. **Scope with the owner.** What decision the results feed, which candidates
   (models × efforts), which delivery modes, budget cap, repeats. Estimate cost
   from a smoke run before committing to a full matrix — high effort plus a
   reviewer costs several dollars per run. → `references/running.md` §Scope
2. **Mine corrections.** `scripts/mine_corrections.py extract` writes every
   message the owner typed after an agent turn; `classify` has a small, cheap
   model (default Claude Haiku) label each one, and `leads` lists the
   corrections. No keyword list decides what counts: the owner often corrects
   with a pointed question. Leads are not cases: read around each, keep judgment
   errors (wrong root cause, skipped verification, stale SOP, scope creep,
   waiting instead of acting), drop style fixes. One case per
   correction moment, not per session. Draft 6–10 candidates into a
   candidates JSON and render `scripts/picker_page.py`; the owner picks 3–5 of
   different error types. → `references/case-authoring.md` §Finding
3. **Gather evidence per picked case**: the owner's verbatim instruction (trace
   back to where the task started), the facts the agent knew that the snapshot
   cannot show, the wrong trajectory, the correction and what was finally built,
   the snapshot SHA from `scripts/find_snapshot.py` (the checkout's reflog, never
   a time-based guess), and every place the answer leaked to later.
   → `references/case-authoring.md` §Evidence
4. **Write `case.json`** (a correction about code with a merged, tested fix
   becomes an implementation case: see §Implementation cases): neutral background, verbatim owner messages,
   attachments, a two-level Trap (`direction`: right way; `pass`: done right),
   yes/no rubric items that each name their evidence, accepted equivalents, leak
   markers absent from the snapshot, the original agent's answer as the
   calibration negative, and — when the agent answered again after the correction
   and the owner accepted it — that answer as the calibration positive. → `references/case-authoring.md` §Writing
5. **Owner review.** `scripts/review_page.py` renders every case exactly as the
   candidate will see it; the owner marks each item agree / change / drop and
   pastes the result back. Apply every mark, then re-render. Publish the page
   (an artifact works well) and keep its source outside temp dirs.
6. **Isolation and calibration.** `evalkit.py check-isolation --case X` must pass
   for every case and `--plant` must fail; `evalkit.py calibrate` must show both
   judges failing both Trap levels on every calibration negative and passing both
   on every calibration positive. A failed positive means the Trap asks for more
   than the owner did, or a judge is too strict — fix that before running models.
   → `references/running.md` §Isolation
7. **Smoke, then the matrix.** A handful of cheap runs through every client path
   first; then `evalkit.py run` with a budget stop, a periodic progress check and
   snapshot cleanup. After any fix to cost accounting, `evalkit.py recompute`.
   → `references/running.md` §Running
8. **Judge, report and archive.** `evalkit.py judge`, then `report`; read raw
   answers before stating any conclusion, and state how small the sample is.
   Archive every finished or stopped batch as Markdown in the repository's report
   folder (for example `docs/agent-eval/`). → `references/reading-results.md` (§Archiving)

Decisions that belong to the owner — scope, budget, case choice, case wording,
whether a conclusion changes an SOP — go to the owner as questions with a
recommendation. Facts (SHAs, prices, which model a gateway serves) are yours to
look up.

## Running the kit

```bash
cp .claude/skills/agent-eval/assets/config.example.json agent-eval/config.json   # then edit
K=.claude/skills/agent-eval/scripts/evalkit.py
uv run --script $K --config agent-eval/config.json check-isolation --case I
uv run --script $K --config agent-eval/config.json calibrate --batch b1
uv run --script $K --config agent-eval/config.json run --batch b1 --parallel 6
uv run --script $K --config agent-eval/config.json judge --batch b1 --parallel 6
uv run --script $K --config agent-eval/config.json report --batch b1
# implementation cases (config `implement`): hidden tests must calibrate first
uv run --script $K --config agent-eval/config.json calibrate-tests --case W
uv run --script $K --config agent-eval/config.json run --batch i1 --cases W --budget 300
# forced adoption of earlier reviews, cheap follow-up:
uv run --script $K --config agent-eval/config.json adopt --batch b2 --from-batch b1 --efforts high --repeats 2
```

Requirements: macOS `sandbox-exec` for Codex isolation, the `claude` and `codex`
CLIs, and either native logins or a gateway (`gateway` in the config). Cases,
config and results can live anywhere; set the paths in the config.

## Before you trust a number

Read `references/pitfalls.md` once per eval. Each item there cost a real batch:
a disk filled by unremoved snapshots, subagent tokens missed so cost was 3×
too low, an effort mismatch that was really a subagent, a Trap no model could
pass, and a "review is useless" conclusion the raw answers contradicted.
