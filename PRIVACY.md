# Privacy

This policy covers the luoy2-skills plugin: the `agent-eval` and
`questionnaire-review` skills in this repository.

In short: the skills use your own session history, on your own computer, to help
you work better with Claude Code. Your session logs stay on your computer, and
nothing is sent to the author.

## What the author collects

Nothing. The plugin has no server, account, analytics or telemetry, and none of
its scripts sends anything to the author.

## What the plugin reads on your computer

- Claude Code session logs (`~/.claude/projects/**/*.jsonl`): questionnaire-review
  reads the questionnaires and your answers; agent-eval reads the messages you
  typed after an agent turn, in the projects you name.
- Codex session logs (`<codex-home>/sessions/`), only when you give agent-eval's
  mining step `--codex-home`.
- Your repository's tracked files and git history at the commits your eval cases
  name (agent-eval).
- The files you pass it: configs, cases and the questionnaire checklist.

## Where data is sent

A step that calls an AI model puts only what the README lists for that step into
the prompt, and sends it only to the provider you choose for that step, through
your own login:

- Anthropic, through the `claude` CLI: the questionnaire classifier, agent-eval's
  classifier (by default), and Claude candidates and judges.
- OpenAI, through the `codex` CLI: agent-eval's classifier when you choose
  `codex:<model>`, and Codex candidates, reviewer and judges.
- Your own model gateway, only if you set `gateway` in agent-eval's config. It
  is off by default, and the plugin supplies no gateway address. It receives the
  prompts of the models routed through it and the key that your `token_command`
  prints.

The README's section "What it reads and where it sends data" lists what each
call contains. Those providers handle the data under their own terms and privacy
policies. The two pages agent-eval generates load fonts from Google Fonts when
you open them.

## Retention

The plugin stores data only in files at paths you choose: extracted rows and
messages, labels, eval snapshots, results and reports. The questionnaire hook
keeps a marker with a timestamp and a tool call id in the system temp folder
until the question is sent again. Delete those files to delete the data; the
plugin keeps no other copy.

## Contact

Questions: https://github.com/luoy2/skills/issues

Security problems, reported privately:
https://github.com/luoy2/skills/security/advisories/new

Last updated: 2026-09-27.
