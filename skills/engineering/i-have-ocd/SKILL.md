---
name: i-have-ocd
description: "Keep one main line and park everything else. The owner feels a strong urge to fix every small detail the moment it appears, and so do agents that report every finding. This skill writes each off-line finding down in one line instead of fixing it or asking about it, keeps questions to main-line decisions, ends every reply with one next step, and plans the parked items in batches when the owner asks. Use it whenever the owner names a main line or says they are lost, scattered or pulled in too many directions, even if they only say 'I have OCD', 'keep me focused', 'one thing at a time', 'what are we doing again', '主线是', '我有点 lost', '分叉太多' or '先记下来'."
---

# I have OCD

Some owners cannot leave a small defect alone: they want it fixed the moment
they see it. Agents make this worse by reporting every finding and asking about
it on the spot. Each detour looks cheap, but together they scatter attention
across many threads. Work that could have been planned together gets done
piecemeal, and the main goal waits. This skill keeps one main line moving. Every
other finding gets written down in one line, and the parked items are planned
together later.

## The store

The state is small: the current main line, and the parked items.

- If the session has focus tools (for example an MCP server exposing
  `focus_get`, `focus_set`, `focus_next`, `park`, `parked_list`, `parked_take`,
  `parked_done`, `parked_drop`), use them. They are shared across machines and
  sessions.
- Otherwise the helper `scripts/ocd.py` next to this file is the only writer.
  It keeps one SQLite store per project in
  `~/.local/state/i-have-ocd/<project>.db`, shared by every session and every
  worktree of the repository on this machine. It is visible only on this
  machine; say so once. `<project>.md` beside it is a generated view: read it
  if you like, but never edit it. An edit left there by a session on the old
  skill is taken into the store by the next command, which reports it.

```bash
O=<this skill's directory>/scripts/ocd.py          # --help lists every option
$O main get                                        # main set --expected-version V --request-id R --next-step …
$O list                                            # pending items, each with id, version and lease
$O park "<one line>" --source <where seen> [--tracking <ticket>] [--needs-owner] --request-id R
$O add-source P3 --source <where> --expected-version V --request-id R
$O take P3 --by <session> --expected-version V --request-id R     # prints a lease token
$O decide P3 --ref <owner's ruling> --expected-version V --request-id R
$O done P3 --kind ticket|pr|scheduled|resolved|transferred|recorded --ref <receipt> --expected-version V --request-id R
$O drop P3 --decision <owner's ruling> --expected-version V --request-id R
$O count                                           # M for the reply marker
```

Pass `--by <session>` on every command, or set `I_HAVE_OCD_BY` once: it names
who acted in the history and who holds a lease. Each item has an id, `P<n>`,
that is never reused; a ticket number or a title is an attribute, so two
concerns on one ticket are two items. Every change names the version you read
and a request id you choose: a retry with the same id returns the first result,
and a stale version is refused with the current state, so read again before you
act. Close an item you took with its `--token`. A missing, locked or damaged
store is an error, never an empty queue. An item stays pending until it is
closed with a receipt (rule 8); a closed item is never reopened, and a
recurrence is a new item citing the old id.

A main line has a title, a done-when criterion (a link to the plan, issue or
milestone that defines it), and the next step. If none is set, ask the owner
which one it is before starting work, as a single question.

## Rules

1. **Read the main line** before you start, after a context compaction, and
   before you ask the owner anything. Before a question that touches parked
   items, list them from the store right then; never ask from a list read
   earlier in the session, since another session may have closed them since.
2. **Classify every new item**: a finding, a background result, a message from
   another agent, or an idea you had.
   - *Main line*: it is inside the done-when criterion, or you can show it
     blocks the main line (a failing check, a missing input, a file:line).
     A small defect in the code the main-line change already touches is
     part of doing that change correctly. Fix it.
   - *Urgent*: real money or position risk, a production incident, a
     leaked credential, or an identity check that needs the owner (a code,
     an interactive login). Raise it now.
   - *Everything else is parked.*
3. **Park, don't fix and don't ask.** Park one line through the helper: what
   it is, why it matters, where you saw it (file:line, run id, issue, message),
   and whether the owner must decide something (`--needs-owner`). Then continue
   the main line. Do not open a ticket, start an agent or send a questionnaire
   for a parked item. If the same concern is already pending, add your source
   to it (`add-source`); a different concern on the same ticket is a new item.
4. **Questions are for main-line decisions and urgent items only.** A question
   about a parked item waits for the review.
5. **End each reply with one next step on the main line**, then
   `side +N · open M`: N items parked in this reply, M the number `count`
   prints after it, or `?` when the store cannot be read. The marker never
   reads as an empty queue while items stay open. Do not list parked items at
   the end of a reply.
6. **The owner's own new idea is not parked.** Do what they ask, and record the
   switch: pause the main line with the reason (`paused for: …`). When the
   detour is done, say so and resume the main line, or ask which line is now
   the main one if the detour grew.
7. **Review in batches, with a questionnaire.** Any question from the owner
   about parked items starts a review ("review side lines", "看支线", "which
   ones are they", "有哪几条"). Group them by area; for each group write a
   short plan: what to do, in what order, what the owner must decide. In the
   same reply, before any fix, list the items from the store and send the
   questionnaire (the client's question tool): one question per item or group,
   naming each item's id, the recommended action first, the other real
   choices, and always a "later" option ("以后再说"). An item answered "later"
   or left unanswered stays pending unchanged; record every ruling with
   `decide`, a taken item included, and close items as rule 8 says. A drop of
   a taken item waits for its lease to expire or goes through its holder. Then
   let the owner pick what becomes the next main line.
8. **Close handled items with a receipt.** Any session that handled an item
   closes it, whoever parked it; the main line stays with the session that set
   it. Take the item first (`take`), so two sessions do not handle it at once.
   An item is handled once it is filed as a ticket or PR (`done --kind ticket`
   or `pr`), scheduled (`scheduled`), done (`resolved`), handed over with a
   durable record of who now owns it (`transferred`), or written into the
   record that carries it (`recorded`); `--ref` names that ticket, PR, schedule
   id or record. An item the owner dropped closes with `drop` and the ruling.
   Focus tools close items with the same receipts. An ACK, a questionnaire
   answer or a mention of an issue number is not a receipt. A "do it" ruling
   is `decide`: the item stays pending until the work is filed, done,
   scheduled or handed over. "Later" or no answer never closes anything.
   Closing an item ends its triage only; it does not mean the ticket's work is
   done. Say in the reply what left the queue
   (`cleared: P3 → #N, P7 dropped`), so nothing disappears unseen.

## Done when

- The main line's done-when criterion is met, and the owner confirms or names
  the next main line.
- Every item reported during the work is either still pending in the store or
  closed with a receipt: a ticket, a PR, a scheduled item, a record, or the
  owner's drop decision. Nothing exists only in chat, and nothing handled stays
  pending.
