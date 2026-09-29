# Reading results

- **Sample size first.** With 3 cases and one run per cell, one rubric item is
  ~14 points. Say so next to every comparison, and repeat top candidates before
  ranking them.
- **Both judges must agree** for a pass; the report lists each disagreement with
  the item's text and both judges' verdicts and evidence, so the owner can rule on
  the wording (one clause read literally by one judge and by purpose by the other
  caused 8 of 11 disputes on one case).
- **Read an implementation case's ceiling before its scores.** Under each table the
  report names the tests the reference passes that no candidate passed, the
  reachable ceiling they leave (scored tests less those), and the candidates'
  range. Tests nobody passes usually encode something the case never disclosed;
  read them before calling the models weak. When the range is below
  `implement.min_spread` (default 3) the case is marked "no discrimination": it does
  not rank the candidates, whatever the means say.
- **Baseline cases are not a comparison.** A case with `"role": "baseline"` sits
  under its own heading: it shows whether an implementer can finish an approved
  plan. Leave it out of model rankings.
- **Timed-out runs stay invalid but keep their tests.** Each arm with timeouts gets
  its own "timed out n/m" row with the tests each run had passed when the clock
  stopped; a high effort that times out at the same score as a finished lower one
  is a cost finding. What invalid runs spent is on its own line under the run cost.
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
