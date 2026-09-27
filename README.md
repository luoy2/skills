# Skills

Agent skills from my own engineering work, straight from my `.claude` directory.
They work with Claude Code and Codex.

Two skills learn from your own session history, on your own computer, to help
you work better with Claude Code: `agent-eval` finds which model and thinking
effort avoid the mistakes your agents actually made, and `questionnaire-review`
turns the way you answer your agents into rules they follow. The third,
`i-have-ocd`, keeps you and your agents on one main line: findings off that line
are written down instead of fixed or asked about on the spot.

Your session logs stay on your computer, and nothing is sent to me: the plugin
has no server, account or telemetry. What leaves your computer is only what a
step puts in a prompt for the AI model you choose (excerpts of your messages, or
an eval case and the files the model reads), sent through your own `claude` or
`codex` login like any prompt you type, or through your own model gateway if you
set one. [Each step is listed below](#what-it-reads-and-where-it-sends-data).

## Install

Claude Code:

```
/plugin marketplace add luoy2/skills
/plugin install luoy2-skills@luoy2
```

Codex and other agents:

```
npx skills@1.7.0 add luoy2/skills
```

## Where it works

`agent-eval` and `questionnaire-review` run on your own computer, in Claude Code
or Codex. They read your local session logs and run the `claude` and `codex` command-line tools, so they
don't work in claude.ai chat or Cowork: the skills load there but can reach
neither. agent-eval's model runs also need macOS, for `sandbox-exec`.
[What each step reads and sends](#what-it-reads-and-where-it-sends-data) is
listed below.

## agent-eval

Public benchmarks don't tell you whether a model will avoid the mistakes your
own agents already make. `agent-eval` builds an eval from those mistakes: every
case is a real moment when you corrected an agent. A candidate model gets the
same instruction, the same facts and the repository as it was then, and is
scored on whether it avoids the mistake. Use it to choose a model and thinking
effort for a role, to test whether a review step or a new `AGENTS.md` actually
helps, and as a regression baseline when a new model ships.

How it works:

1. **Mine corrections.** Every message you typed after an agent turn is
   extracted from your Claude Code and Codex session logs, and a small, cheap
   model (Claude Haiku by default) labels each one: a correction or not, what
   kind of mistake, a one-line summary. No keyword list decides, because people
   often correct an agent with a pointed question. The agent keeps judgment
   errors (wrong root cause, skipped verification, waiting instead of acting,
   scope creep), and you pick 3–5 of different kinds on a generated page.
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
   `sandbox-exec`: outside the run's own folder it cannot read files under the
   config's `deny_roots` (by default `/Users`, `/Volumes` and `/private/tmp`)
   or write anywhere but the system temp folders. It can still use the network.
   A planning Claude run gets read-only tools on the snapshot. Implementation
   runs turn off the candidate's permission prompts so it can edit the snapshot
   (Claude `--dangerously-skip-permissions`, Codex `danger-full-access`); they
   always run inside `sandbox-exec`, and the kit refuses to start a
   write-capable Claude that isn't. Both judges (from different vendors) must
   fail the original wrong answer and pass the answer you accepted before any
   model is scored.
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
your new `AGENTS.md` or SOP files laid over the old snapshots. Every prompt,
report and page string comes from one text table; to run in another language,
name a translated table as `text` in the config.

Requirements: macOS (for `sandbox-exec`), [uv](https://docs.astral.sh/uv/), the
`claude` and `codex` CLIs with native logins, or your own
OpenAI/Anthropic-compatible gateway. Start with
`skills/engineering/agent-eval/SKILL.md`; the agent walks you through scoping,
case writing and the run.

## questionnaire-review

Claude Code agents ask you to decide through multiple-choice questionnaires,
with one option marked recommended. Each time you pick something else, the
agent's default judgment and yours differed, and those differences are not
random: they cluster by what the recommendation does and by theme.
`questionnaire-review` finds them in your session logs every two weeks and turns
the recurring ones into rules you accept one by one.

How it works:

1. **Extract.** Every questionnaire and your answer come out of
   `~/.claude/projects`: the options, which one was recommended, what you chose
   or wrote instead, and how long you took to answer.
2. **Label.** A small model (Claude Haiku by default) labels what each
   recommended option does (act now, defer, keep the old path, hand you a manual
   step, file a ticket, split the work, …) and, for an override, what you chose
   and what the difference is about. The counting stays in the script.
3. **Read and audit.** The agent reads every override and every answer you wrote
   yourself, groups them into themes, and checks each theme against your
   project's rules: covered, partly covered or missing, with file:line.
4. **Report.** A page shows acceptance overall and by recommended action, the
   themes with dated examples, and the change since the previous round's rules.
5. **Decide.** One question per candidate rule. Accepted rules go into your
   project's rules in one PR; declined ones are recorded so the next round skips
   them.
6. **Enforce.** The first round adds a short checklist and a PreToolUse hook:
   before each questionnaire is shown, the agent gets the checklist back and
   rewrites its question against it. The hook is registered in the project's
   `.claude/settings.json`, so every checkout, machine and cloud session gets it.

Raw rows and quotes are stored only on your machine; the classifier call sends
excerpts to Anthropic (see below), and the PR carries counts and rule text.

Requirements: Claude Code, Python 3.11+ or [uv](https://docs.astral.sh/uv/),
`node`, and the `claude` CLI for the classifier. Start with
`skills/engineering/questionnaire-review/SKILL.md`.

## i-have-ocd

Some people can't leave a small defect alone; they want it fixed the moment they
see it. Agents feed that urge by reporting every finding and asking about it
right away. Each detour looks cheap. Together they scatter your attention, and
work that should have been planned together gets done piecemeal. `i-have-ocd`
keeps one main line: a title, a done-when criterion and the next step.

- Every new item is sorted as main line, urgent, or parked.
- Urgent means money or position risk, a production incident, a leaked
  credential, or an identity check.
- A parked item is written down in one line: what it is, why it matters and
  where it was seen. It is not fixed or asked about on the spot.
- Questions are only about main-line decisions.
- Each reply ends with one next step and a `side +N` count.
- Your own new ideas are done right away, and the main line is recorded as
  paused.
- When you ask for a review, parked items are grouped and planned in batches,
  and the decisions come as one set of questions.

It keeps its state in focus tools if your session has them, for example an MCP
server shared by all your machines. Otherwise it uses one Markdown file per
project under `~/.local/state/i-have-ocd/`. It reads no logs and sends nothing.
Start with `skills/engineering/i-have-ocd/SKILL.md`.

## What it reads and where it sends data

Your session logs stay on your computer, and nothing goes to me: the plugin has
no server, account or telemetry. A step that calls an AI model puts only what
its row lists into the prompt, and sends it only to the model you chose for that
step, through your own `claude` or `codex` login. Those providers handle it
under their own terms, as they do your other prompts.

agent-eval can also send its model calls through a gateway. It is off unless
your config sets `gateway`, and it is meant for a model proxy you run or use
yourself, for example when you reach some models only through one. The plugin
never supplies a gateway address.

Each step reads and writes only what its row says. A step that isn't listed
reads only the files you pass it and sends nothing.

### questionnaire-review

| Step | Reads | Sends, and to whom | Writes |
| --- | --- | --- | --- |
| `questionnaires.py extract` | Claude Code session logs: `~/.claude/projects/**/*.jsonl`, or the folder given as `--projects` | Nothing | One row per question (the question, its options, your answer, timestamps) to the `--out` file |
| `questionnaires.py classify` | Those rows | Anthropic, through `claude -p` with no tools (default model Claude Haiku), 25 questions per call: each question (first 600 characters), its options, which one was recommended, your answer (first 600 characters) and whether you took the recommendation | Labels to the `--labels` file |
| `questionnaires.py stats`, `leads` | Rows and labels | Nothing | Standard output; `leads` replaces secret-shaped strings with `[redacted]` |
| `decision-check.mjs` hook | The questionnaire Claude Code is about to show (the hook's input) and your checklist file | Nothing over the network. The denial returns the checklist to Claude, so its text becomes part of that conversation | A marker holding a timestamp and the tool call id in the system temp folder (`DECISION_CHECK_STATE_DIR` changes it); the retry removes it |

The hook runs only in a project where you copied it and registered it in
`.claude/settings.json`
([setup](skills/engineering/questionnaire-review/references/setup.md));
installing the plugin doesn't register it.

### agent-eval

| Step | Reads | Sends, and to whom | Writes |
| --- | --- | --- | --- |
| `mine_corrections.py extract` | Logs of the Claude Code projects named with `--claude-project`; with `--codex-home`, Codex sessions (`<codex-home>/sessions/**/rollout-*.jsonl`) whose working directory starts with `--codex-cwd` | Nothing | Each message you typed after an agent turn (first 4,000 characters), the end of that agent turn (last 1,500) and your previous message (first 600), to the `--out` file |
| `mine_corrections.py classify` | Those messages | The classifier you pick, 20 messages per call: Anthropic through `claude -p` with no tools (default, Claude Haiku), or OpenAI through `codex exec` in read-only mode (`--classifier codex:<model>`). Per message: your text (first 1,500 characters), the end of the agent turn (last 1,200) and your previous message (first 600) | Labels to the `--labels` file |
| `find_snapshot.py` | A checkout's git reflog | Nothing | Standard output |
| `picker_page.py`, `review_page.py` | Candidate and case files | Nothing. The pages they write load the IBM Plex fonts from Google Fonts when you open them | An HTML page |
| `evalkit.py run`, `adopt`, `judge`, `calibrate` | The config, the cases, and your repository's tracked files at each case's commit (`git archive`) | The models in the config (candidates, reviewer, judges), through `claude` (Anthropic), `codex` (OpenAI) or, if you set one, your own gateway. A candidate gets the case prompt (the messages, facts and attachments written in the case) and reads files from the snapshot; the reviewer gets the case and the candidate's answer; a judge gets the case, its Trap and rubric and the answer, plus the diff and test results for an implementation case. Your gateway also gets the key that your `token_command` prints | Snapshots and each run's output under `scratch_root`; ledgers and reports under `results_dir` |
| `evalkit.py check-isolation`, `calibrate-tests`, `recompute`, `report`, `text` | The config, cases, snapshots and results | Nothing | Files under `scratch_root` and `results_dir`, reports |

A Codex model and an implementation run start with a minimal environment:
`PATH`, your gateway key if you use one, and `HOME` and `TMPDIR` inside the
run's folder. A
Claude planning run or Claude judge keeps your environment minus
`ANTHROPIC_API_KEY` and the variables that start with the config's
`forbidden_env_prefixes` (by default `GH_`, `GITHUB_`, `OP_`, `AWS_` and
`SSH_AUTH_SOCK`), so it can use your `claude` login. `uv` downloads Python 3.11
or later if your machine has none; the scripts import only the standard library.

## Privacy and support

[Privacy policy](https://github.com/luoy2/skills/blob/main/PRIVACY.md). Questions and problems:
[GitHub issues](https://github.com/luoy2/skills/issues). Security problems:
[report privately](https://github.com/luoy2/skills/security/advisories/new).

## License

MIT
