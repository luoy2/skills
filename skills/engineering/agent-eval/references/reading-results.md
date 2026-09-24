# Reading results

- **Sample size first.** With 3 cases and one run per cell, one rubric item is
  ~14 points. Say so next to every comparison, and repeat top candidates before
  ranking them.
- **Both judges must agree** for a pass; list disagreements for the owner.
- **A Trap nobody passes carries no signal.** First check the calibration positive:
  if a judge fails the owner-accepted answer, the condition or the judge is the
  problem, not the models. Check the `direction` level; if that
  is also zero, the pass condition or the case may be too demanding, or the
  models genuinely miss it — read answers to tell which.
- **Read raw answers before any conclusion.** An average can say "revision after
  review scores lower" while the review texts show the reviewer named the right
  direction almost every time, and a forced-adoption batch shows authors who adopted
  every point still choosing the cautious default, because the same reviews also
  raised the risks that justify caution. Cases built from an owner's corrections
  often test that owner's engineering preferences, which neither the repository nor
  the reviewer states. Search answers and reviews for the key
  idea (regex over `final`, `draft` and the review stage), quote judge evidence, and
  ask whether the missing knowledge is written anywhere the candidate could read.
- **Cost per useful result**, not per token: report the list-price chain cost per
  run (candidate + reviewer + subagents) next to scores; judges separately.
- **Effort ladders are rarely monotonic.** Report the curve per model; flag where
  cost multiplies without score change.
- **Report shape**: headline facts (runs, validity, trap results, spend), 3–5
  conclusions each with its evidence, a table per model × effort × mode, what the
  data cannot say, suggested next steps as options for the owner. Post the report
  link and receipts (commit, commands, data paths, spend, isolation and calibration
  results, NOT-RUN parts) on the tracking issue.

## Archiving

A published page is for reading now; the archive is what the next eval and the
owner can cite later. Every finished or stopped batch gets archived:

1. `evalkit.py report --batch <b> --md <file>` writes the same tables as the HTML
   report as Markdown (plans shared by implementers are counted once in spend;
   batches judged before the two-level Trap show `—` for direction).
2. Put it in the repository's report folder (for example `docs/agent-eval/`, named
   `<first run date>-<plan|impl>-eval.md`). A follow-up batch of the same eval goes
   into that file under an "Addendum" section; a new eval gets a new file.
3. Above the tables write, by hand: a dated-evidence banner (not a rule; role
   bindings change only by the owner), what was tested, 3–5 conclusions each with
   its numbers, and what the sample cannot say. Where the raw data lives, but no
   raw data itself.
4. Index it wherever your repository lists documents, commit through your usual
   review path, and link the archived file from the tracking issue.
