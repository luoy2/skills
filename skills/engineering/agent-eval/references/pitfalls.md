# Pitfalls (each happened in a real first batch)

| What happened | How it showed | What prevents it |
|---|---|---|
| Snapshot SHA guessed from time | 2 of 3 cases would have used the wrong commit | `find_snapshot.py` on the checkout's reflog |
| Attachment was an untracked draft since deleted | Could not rebuild the case from git | Restore from the transcript's Read result |
| Attachment header explained the case | Hint visible to candidates | Strip authoring notes from attachments |
| Your own memory notes contained the answers | Leak into any unisolated run | Isolation denies the whole home tree; leak markers include eval-internal names |
| Codex sandbox denied all reads under /private/tmp | Codex aborted at start (`exit -6`, canonicalize CODEX_HOME) | Deny `file-read-data`, not `file-read*` |
| Codex read-only sandbox still reads everything | Candidate read scratch notes with the answer | Wrap Codex in `sandbox-exec` |
| Snapshots never removed (340 MB each) | Disk full at 109 runs; runner crashed with ENOSPC | Runner deletes `wt/` per run; monitor free disk |
| Fresh CODEX_HOME unpacks ~90 MB `.tmp` | 20 GB of judge dirs in an evening | Runner deletes `.tmp` and judge workdirs |
| Codex subagents write separate rollouts | Cost under-counted 2–4× on ultra runs; false effort mismatch | Bill every rollout; effort from the main thread; `recompute` |
| Gateway did not serve the requested model | 429 cooling down / model absent | Probe each model before the batch; native fallback needs owner approval |
| Pass condition too strict, no direction level | No candidate passed any Trap, so no signal | Two-level Trap |
| An isolated planner named the fix's test file by the repository's convention | Every implementer sharing that plan was rejected on a leak marker | Leak-check the task prompt alone; record marker hits in the plan |
| Only negative calibration | A judge failed even the owner-accepted answer at the pass level, unnoticed until a positive was added | Calibrate with a positive as well as a negative |
| Average read as "review hurts" | SOP paused on a within-noise difference | Read raw reviews and answers before concluding |
| `json.dump` rewrote a hand-formatted shared JSON | Unrelated diff noise | Insert text instead of re-serializing |
| `git stash` in a worktree a batch was reading | Runner briefly saw old source | Never stash under a running batch |
| Copying a file into a linked worktree's `.git` | Overwrote the gitdir pointer file | `.git` in a linked worktree is a file |
| Results stored inside a worktree | Deleting the worktree would delete results | Put `results_dir` outside worktrees |
