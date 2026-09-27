---
name: questionnaire-review
description: "Every two weeks, review the multiple-choice questions (AskUserQuestion) your Claude Code agents asked you: how often you took the recommended option, where you overrode it and why, and which of those differences deserve a written rule. Extracts every questionnaire and answer from the session logs, has a small model label what each recommendation did, shows the numbers and themes on a report page, asks one question per candidate rule, writes the accepted rules into the project's rules, and installs a hook that makes agents re-read those rules before every questionnaire. Use it for the biweekly review and whenever someone asks how often they accept their agents' recommendations, what they keep overriding, how their judgment differs from the agents', or wants the agents to ask better questions, even if they only say 'review my answers', 'why do I keep overriding you', '问卷复盘' or '我的偏好'."
---

# Questionnaire review

An agent's recommended option is its default judgment; the owner's answer is the
owner's. Every override marks a place where the two differ, and the overrides are
not random: they cluster by what the recommendation does (defer, keep the old
path, hand the owner a manual step, file a ticket) and by theme. Once a
difference is written down as a rule, the agents stop recommending against it.
This skill measures that, turns the recurring differences into rules the owner
accepts one by one, and adds a hook so the rules are read before each question.

## What you need

- Claude Code session logs on the machine where the owner answers
  (`~/.claude/projects/**/*.jsonl`). Codex asks in prose and records no
  structured answer, so it is out of scope.
- Python 3.11+ (or `uv`), `node`, and the `claude` CLI for the small classifier.
- The project where the rules live: its `AGENTS.md` or `CLAUDE.md` and any
  development docs they route to.

In the first round, set up the checklist and the hook as `references/setup.md`
describes, and ship them in the same PR as the first rules.

## A round

1. **Window.** Start where the previous round ended (its PR states it), or 14
   days back.
2. **Earlier rounds.** Read the previous rounds' PRs and the owner's answers on
   them. Do not raise again a rule the owner declined.
3. **Numbers.** Run `scripts/questionnaires.py` (`--help` lists the options):
   - `extract --since <window start> --out <private>/rows.jsonl`
   - `classify --rows … --labels …`: a small model labels the recommended
     action, the chosen action and the theme of each override. It exits 1 while
     anything is unlabelled, a failed batch included; rerun it, and report what
     the model still skips after a rerun as unlabelled.
   - `stats --rows … --labels … --split <previous round's merge time>`
   - `leads --rows … --labels …`, with `--tz <zone>` when the owner reads dates
     in another time zone than this machine's
4. **Read every lead in full**: each changed answer, own answer and declined
   questionnaire. The labels sort the leads; they do not replace reading them.
   Group the overrides into themes, each with a count and dated examples in the
   owner's words.
5. **Audit** each theme against the project's rules: mark it covered, partly
   covered or missing with file:line, and draft the rule and the file it belongs
   in. `references/report.md` explains how to read the numbers and lays out the
   audit table.
6. **Report page** for the owner, laid out as `references/report.md` describes.
7. **Ask** one question per candidate rule, recommendation first. The owner may
   rewrite a rule or decline it; record either.
8. **Write** the accepted rules: rules about how to ask the owner go into the
   checklist, others into the file the project keeps that rule in. Deliver one PR.

## Privacy

Rows, labels and the report page hold the owner's words and the agents' text.
Keep them in a private scratch directory, never in the repository. The PR carries
counts and rule text only. Quote the owner only from `leads` output, which masks
secret-shaped strings, and leave out personal matters.

## Deliver

One PR per round, titled `docs: questionnaire review round N — <main themes>`.
The body:

- the window, questionnaire and question counts, and acceptance overall and by
  recommended action against the previous round;
- a change table: rule · evidence (count and dates) · file:line;
- the candidates the owner declined, with their question, so the next round
  skips them;
- the checks run and their results.

A round with no new rule opens no PR; it reports its numbers in its output.

Run a round every two weeks. It starts by hand: the owner asks, or a session
that finds the last round's PR more than 14 days old offers it.
