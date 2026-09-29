#!/usr/bin/env node
// PreToolUse hook for AskUserQuestion: agents re-read the project's checklist for
// questions to the owner before each questionnaire is shown.
//
// Register it in the project's .claude/settings.json in exec form, with the
// checklist path (relative to the project root) as the first argument and,
// optionally, `--lang zh` for a Chinese lead line (the default is `en`):
//
//   { "matcher": "AskUserQuestion",
//     "hooks": [{ "type": "command", "command": "node",
//                 "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/decision-check.mjs",
//                          "docs/owner-decisions.md"] }] }
//
// Each questionnaire is denied once at first, and the reason handed back to
// the model is the checklist in full, so the stem and the recommended options are
// checked before the owner sees them. The model's retry from the same session
// within RETRY_WINDOW_MS goes through and clears the marker; the next questionnaire
// is checked again. A marker older than the window belongs to a questionnaire that
// was never re-sent, and counts as absent.
//
// Every failure (unreadable input, missing checklist, unknown language, unwritable
// state directory) lets the questionnaire through and says why in a systemMessage:
// this check must never keep the owner from being asked.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';

const RETRY_WINDOW_MS = 10 * 60 * 1000;
const DEFAULT_CHECKLIST = join('docs', 'owner-decisions.md');
// The line in front of the checklist, in the owner's language: the owner sees the
// denial too.
const LEADS = {
  en: (minutes) =>
    'Before asking, check the question and each recommended option against the checklist below, ' +
    `then send the questionnaire again; a resend from this session within ${minutes} minutes goes through.`,
  zh: (minutes) =>
    '发问卷前先按下面的清单逐条核对题干和推荐项，改好后重新发这份问卷；' +
    `同一会话 ${minutes} 分钟内的重发会放行。`,
};

const emit = (payload) => process.stdout.write(JSON.stringify(payload));
const letThrough = (why) => {
  emit({ systemMessage: `decision-check: ${why}; questionnaire sent unchecked` });
  process.exit(0);
};

let input;
try {
  input = JSON.parse(readFileSync(0, 'utf8'));
} catch {
  letThrough('unreadable hook input');
}
if (input.tool_name !== 'AskUserQuestion') process.exit(0);

let named;
let lang = process.env.DECISION_CHECK_LANG || 'en';
const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--lang') lang = args[++i];
  else if (args[i].startsWith('--lang=')) lang = args[i].slice('--lang='.length);
  else named ??= args[i];
}
if (!Object.hasOwn(LEADS, lang)) letThrough(`unknown --lang ${lang} (use ${Object.keys(LEADS).join(' or ')})`);

const root = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
named ||= process.env.DECISION_CHECKLIST || DEFAULT_CHECKLIST;
const checklistPath = isAbsolute(named) ? named : join(root, named);
let checklist;
try {
  checklist = readFileSync(checklistPath, 'utf8');
} catch {
  letThrough(`${checklistPath} not found`);
}

const stateDir = join(process.env.DECISION_CHECK_STATE_DIR || tmpdir(), 'decision-check');
const session = String(input.session_id || 'unknown').replace(/[^A-Za-z0-9_-]/g, '_');
const marker = join(stateDir, `${session}.json`);

let pending = null;
try {
  pending = JSON.parse(readFileSync(marker, 'utf8'));
} catch {
  // No questionnaire of this session is waiting for its retry.
}
if (pending && Date.now() - Number(pending.denied_at) < RETRY_WINDOW_MS) {
  rmSync(marker, { force: true });
  process.exit(0);
}

try {
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(marker, JSON.stringify({ denied_at: Date.now(), tool_use_id: input.tool_use_id ?? null }));
} catch (error) {
  letThrough(`cannot record the pending check (${error.message})`);
}

emit({
  hookSpecificOutput: {
    hookEventName: 'PreToolUse',
    permissionDecision: 'deny',
    permissionDecisionReason: `${LEADS[lang](RETRY_WINDOW_MS / 60000)}\n\n${checklist}`,
  },
});
