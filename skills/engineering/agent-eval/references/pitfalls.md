# Pitfalls (each happened in a real first batch)

| What happened | How it showed | What prevents it |
|---|---|---|
| Snapshot SHA guessed from time | Most cases would have used the wrong commit | `find_snapshot.py` on the checkout's reflog |
| Attachment was an untracked draft since deleted | Could not rebuild the case from git | Restore from the transcript's Read result |
| Attachment header explained the case | Hint visible to candidates | Strip authoring notes from attachments |
| Your own memory notes contained the answers | Leak into any unisolated run | Isolation denies the whole home tree; leak markers include eval-internal names |
| Codex sandbox denied all reads under /private/tmp | Codex aborted at start (`exit -6`, canonicalize CODEX_HOME) | Deny `file-read-data`, not `file-read*` |
| Codex read-only sandbox still reads everything | Candidate read scratch notes with the answer | Wrap Codex in `sandbox-exec` / `bwrap` |
| Snapshots never removed (a full checkout each) | Disk full partway through the batch; runner crashed with ENOSPC | Runner deletes `wt/` per run; monitor free disk |
| Fresh CODEX_HOME unpacks a large `.tmp` | Tens of GB of judge dirs in one evening | Runner deletes `.tmp` and judge workdirs |
| Codex subagents write separate rollouts | Cost under-counted several times over on high-effort runs; false effort mismatch | Bill every rollout; effort from the main thread; `recompute` |
| Gateway did not serve the requested model | 429 cooling down / model absent | Probe each model before the batch; native fallback needs owner approval |
| Pass condition too strict, no direction level | No candidate passed any Trap, so no signal | Two-level Trap |
| An isolated planner named the fix's test file by the repository's convention | Every implementer sharing that plan was rejected on a leak marker | Leak-check the task prompt alone; record marker hits in the plan |
| A shared leak marker matched the runner's own paths (`agent-eval` in the scratch root) | Every run invalid before it started | Pick markers that cannot appear in `scratch_root`, `allow_read` or the sandbox profile |
| Only negative calibration | A judge failed even the owner-accepted answer at the pass level, unnoticed until a positive was added | Calibrate with a positive as well as a negative |
| Average read as "review hurts" | SOP paused on a within-noise difference | Read raw reviews and answers before concluding |
| `json.dump` rewrote a hand-formatted shared JSON | Unrelated diff noise | Insert text instead of re-serializing |
| `git stash` in a worktree a batch was reading | Runner briefly saw old source | Never stash under a running batch |
| Copying a file into a linked worktree's `.git` | Overwrote the gitdir pointer file | `.git` in a linked worktree is a file |
| Results stored inside a worktree | Deleting the worktree would delete results | Put `results_dir` outside worktrees |
| Hidden tests asserted literal Chinese sentences from the real fix's messages (W) | Every implementer failed the same 3–4 tests; ceiling 38–39/42 | `lint-case`: an asserted string must be disclosed; tests assert behaviour |
| Hidden tests required names, defaults and states the approved plan never gave or contradicted (Q) | 12–18 tests failed for every implementer; range 265–271 under a ceiling of 271–273 | `lint-case` name check and given-plan docstring list; report ceiling and "no discrimination"; `role: baseline` |
| A fake broker contract carried only `conId` (W `FakeUnderlying`) | A run reading `secType`, as production code may, crashed | A fake exposes the real type's public fields, or `interface` lists them; `alt_patches` catches the crash |
| The only plans that passed were isomorphic to the reference (W, Fable) | Another reasonable plan's implementation was graded red for its design | `alt_patches` in `calibrate-tests`; `reference_only` for design-bound tests |
| An attachment said to keep what the Trap penalised keeping (A) | 0 passes; the material itself pointed the wrong way | `trap.wrong_markers` lint; a required calibration positive |
| `direction` said "as the default, not as an option with preconditions" (H) | Judges split on the same answer | Direction states a property; `lint.direction_forbidden`; clause-list `trap.pass` |
| The background carried an earlier owner ruling the Trap penalised following (I) | Candidates that obeyed the owner failed | Mark superseded rulings or remove them; a required positive shows the Trap passable |
| A shared "no side effects" item was applied to implementation (C1 on Q) | An implementer was failed for the config change its plan ordered | Rubric `kinds` |
| Timed-out implementation runs were dropped from the report | Four runs at 267–269/285 vanished, hiding that high effort bought nothing | Timed-out rows with test counts; `implement.timeouts` per effort |
| Judge disputes were listed by run id only | The owner could not see which clause the judges read differently | Dispute list with item text and each judge's evidence |
| Cases were run because each judge failed the negative and the reference passed | All five cases measured the wrong thing | The calibration gate: `run` needs passing judge and test records of the current case digest |
