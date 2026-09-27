# First round: the checklist and the hook

Do this once per project, in the first round's PR.

## The checklist

1. Put it where the project keeps its development rules, for example
   `docs/owner-decisions.md`, and link it from the project's `AGENTS.md` or
   `CLAUDE.md` in one line so it has one home.
2. Start from `assets/owner-decisions.example.md`, then keep only the rules the
   owner accepts in this round's questions. Each rule should come from a pattern
   in the owner's answers, not from the example.
3. Keep it short. The hook sends the whole file to the model before every
   questionnaire, so every line costs tokens on every question.

## The hook

1. Copy `scripts/decision-check.mjs` into the project as
   `.claude/hooks/decision-check.mjs`. Keeping it in the repository means every
   checkout, machine and cloud session gets the same check.
2. Register it in the project's `.claude/settings.json` (merge with what is
   there; `assets/settings.example.json` shows the entry). Use exec form,
   `"command": "node"` with the script and checklist path in `"args"`: it runs
   without a shell, so `${CLAUDE_PROJECT_DIR}` needs no quoting and behaves the
   same on Windows.
3. Commit both.

Why the project settings and not `~/.claude/settings.json`: user settings are
per machine and are not synced, and a cloud session reads only the settings
committed to the repository.

## Before tracking `.claude/settings.json`

Check each machine's checkout for an untracked, ignored `.claude/settings.json`
(`git status --ignored .claude`). Claude Code writes project-scope settings there,
such as plugins enabled for this project. Once the repository tracks the file, the
next checkout overwrites the local one without a warning, and those settings are
gone. Move them first: to `~/.claude/settings.json`, or to
`.claude/settings.local.json` when they should stay with this project on this
machine. Afterwards, enable plugins at user or local scope.

## Check that it works

`claude -p` sessions have no AskUserQuestion, so the hook can only be seen in an
interactive session:

1. Start `claude` in the project (a `tmux` session works for an unattended check)
   and ask it to send one questionnaire. In a folder Claude Code has not seen,
   the trust prompt comes first and its default is "No, exit": press Down, then
   Enter.
2. The first call is denied: the terminal shows
   `PreToolUse:AskUserQuestion hook error` followed by the checklist. The model
   rewrites the question and sends it again, and this time it is shown.
3. The session log (`~/.claude/projects/<project>/<session>.jsonl`) holds the
   evidence: an AskUserQuestion tool_use, its tool_result with `is_error: true`
   and the checklist text, then a second tool_use that was answered.

The owner sees that denial line before every questionnaire, and each
questionnaire costs one extra round trip. The hook fails open: a missing
checklist, unreadable input or an unwritable state directory lets the question
through with a system message saying why.
