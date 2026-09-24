# Skills

Agent skills from my own engineering work, straight from my `.claude` directory.
They work with Claude Code and Codex.

## Install

Claude Code:

```
/plugin marketplace add luoy2/skills
/plugin install luoy2-skills@luoy2
```

Codex and other agents:

```
npx skills@latest add luoy2/skills
```

## agent-eval

Public benchmarks don't tell you whether a model will avoid the mistakes your
own agents already make. `agent-eval` builds an eval from those mistakes: every
case is a real moment when you corrected an agent. A candidate model gets the
same instruction, the same facts and the repository as it was then, and is
scored on whether it avoids the mistake. Use it to choose a model and thinking
effort for a role, to test whether a review step or a new `AGENTS.md` actually
helps, and as a regression baseline when a new model ships.

How it works:

1. **Mine corrections.** Scan your Claude Code and Codex session logs for
   messages where you pushed back on an agent. Keep judgment errors (wrong root
   cause, skipped verification, waiting instead of acting, scope creep); drop
   typos. You pick 3–5 of different kinds on a generated page.
2. **Write each case.** Your verbatim messages, a neutral statement of the facts
   the agent knew, the commit it was working on (recovered from the checkout's
   reflog, not guessed from time), and a two-level **Trap**: `direction` (the
   right way) and `pass` (done right). Add yes/no rubric items, each naming its
   evidence, and leak markers: strings from the later fix that must not appear
   anywhere the candidate can read.
3. **Review the cases.** An annotatable page shows each case exactly as the
   candidate will see it; you mark every item agree / change / drop.
4. **Isolate and calibrate.** Each run gets a fresh one-commit snapshot with no
   later history, no credentials and no memory files. Codex runs inside macOS
   `sandbox-exec`; Claude runs restricted to read-only tools on the snapshot.
   Both judges (from different vendors) must fail the original wrong answer and
   pass the answer you accepted before any model is scored.
5. **Run the matrix.** Planning cases score an answer plus a delivery plan.
   Implementation cases let the candidate edit code, then score it with the
   real fix's own tests, placed only after the candidate stops. Delivery modes:
   `solo`, `review` (a fixed reviewer, then revision), `adopt` (forced adoption
   of the review), and for implementation `direct`, `split-<planner>` (one
   shared plan per case) and `given-plan`.
6. **Report with cost.** Both judges must agree for a pass; disagreements go to
   you. Each run's list-price cost covers the whole chain, including the
   reviewer and any subagents the candidate spawned.

To measure an instruction change, set `overlay_dir`: the same cases rerun with
your new `AGENTS.md` or SOP files laid over the old snapshots.

Requirements: macOS (for `sandbox-exec`), [uv](https://docs.astral.sh/uv/), the
`claude` and `codex` CLIs with native logins, or an OpenAI/Anthropic-compatible
gateway. Start with `skills/engineering/agent-eval/SKILL.md`; the agent walks
you through scoping, case writing and the run.

## License

MIT
