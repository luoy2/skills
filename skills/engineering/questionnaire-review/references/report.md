# The round's report and audit

## Reading the numbers

- **Acceptance** counts only single-choice questions that had a recommended
  option and that the owner answered: took the recommendation, picked another
  option, or wrote their own answer. Multi-select questions, questions without a
  recommendation, and declined or dismissed questionnaires are reported beside it.
- **Answer time.** When taking the recommendation takes as long as overriding
  it, acceptance is read, not reflex. A much faster acceptance suggests the
  questions are routine enough to stop asking.
- **Which option an override took.** Overrides that mostly pick the agent's
  second option mean the agent saw the owner's answer and ranked it lower: the
  fix is a rule about how to rank, not more options.
- **Acceptance by recommended action.** The actions with the lowest acceptance
  (in practice often deferring, keeping the old path, handing the owner a manual
  step, filing a ticket) are where a written rule pays off. Compare against the
  previous round, and split the window at the time the previous round's rules
  merged (`stats --split`); small samples show direction only.
- **Owner's own answers** that ask "what does this mean" or "why" point at the
  question, not the recommendation: the agent asked before explaining the
  mechanism, or the question rested on a stale fact.

The model's labels sort the leads. Spot-check about fifty against the questions
before trusting a category's numbers; the usual confusion is whose voice an
option is written in ("I'll add it" said by the owner is the owner acting).

## The report page

An HTML page for the owner, in this order:

1. The window and the counts; acceptance as one stacked bar (took / picked
   another / own answer), with answer-time medians and declined questionnaires.
2. One sentence on each side: what the agents' recommendations default to, and
   what the owner's choices favour.
3. Acceptance by recommended action as a bar chart, with the overall rate as a
   reference line.
4. A table of the themes: the agents' default, the owner's preference, the design
   pattern or principle behind each side, and the evidence count.
5. One section per theme: the two defaults side by side, the numbers, and a table
   of dated examples (the question, the recommendation, the owner's answer in the
   owner's words). Include counter-examples where the owner chose the other way.
6. Before and after the previous round's rules.
7. The audit table, then the owner's decisions once they are made.

Write labels and explanations in the owner's language. Quote only text that
`leads` printed (it masks secrets) and leave out personal matters.

## The audit table

One row per candidate rule:

| Column | What goes in it |
|---|---|
| Candidate rule and evidence | The rule in one sentence; the count and one or two dated examples |
| Current rules | Covered, partly covered or missing, with file:line of what the project already says |
| Proposal | The wording and the file it belongs in, or "do not write" with the reason |

A difference the owner settles case by case is not a rule. Ask one question per
row, recommendation first, and record the declined rows in the PR so the next
round does not raise them again.
