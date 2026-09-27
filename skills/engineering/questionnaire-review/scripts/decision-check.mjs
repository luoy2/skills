#!/usr/bin/env node
// PreToolUse hook for AskUserQuestion: agents re-read the project's checklist for
// questions to the owner before each questionnaire is shown.
//
// Register it in the project's .claude/settings.json in exec form, with the
// checklist path (relative to the project root) as the first argument:
//
//   { "matcher": "AskUserQuestion",
//     "hooks": [{ "type": "command", "command": "node",
//                 "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/decision-check.mjs",
//                          "docs/owner-decisions.md"] }] }
//
// The first questionnaire a session sends is denied, and the reason handed back to
// the model is the checklist in full, so the stem and the recommended options are
// checked before the owner sees them. The model's retry from the same session
// within RETRY_WINDOW_MS goes through and clears the marker; the next questionnaire
// is checked again. A marker older than the window belongs to a questionnaire that
// was never re-sent, and counts as absent.
//
// Every failure (unreadable input, missing checklist, unwritable state directory)
// lets the questionnaire through and says why in a systemMessage: this check must
// never keep the owner from being asked.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';

const RETRY_WINDOW_MS = 10 * 60 * 1000;
const DEFAULT_CHECKLIST = join('docs', 'owner-decisions.md');

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

const root = process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd();
const named = process.argv[2] || process.env.DECISION_CHECKLIST || DEFAULT_CHECKLIST;
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
    permissionDecisionReason:
      'Before asking, check the question and each recommended option against the checklist below, ' +
      `then send the questionnaire again; a resend from this session within ${RETRY_WINDOW_MS / 60000} ` +
      `minutes goes through.\n\n${checklist}`,
  },
});
