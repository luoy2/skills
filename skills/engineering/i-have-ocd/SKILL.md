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

The state is small: the current main line, and a queue of parked items.

- If the session has focus tools (for example an MCP server exposing
  `focus_get`, `focus_set`, `focus_next`, `park`, `parked_list`, `parked_take`,
  `parked_done`, `parked_drop`), use them. They are shared across machines and
  sessions.
- Otherwise use one Markdown file per project at
  `~/.local/state/i-have-ocd/<project>.md`. It has a `## Main line` section
  (title, done-when, next step, paused-for) and a `## Parked` list, one item per
  line. It is visible only on this machine; say so once when you create it.

The store holds open work only. A parked item leaves it once it is handled
(rule 8), so the store stays small however long the work runs.

A main line has a title, a done-when criterion (a link to the plan, issue or
milestone that defines it), and the next step. If none is set, ask the owner
which one it is before starting work, as a single question.

## Rules

1. **Read the main line** before you start, after a context compaction, and
   before you ask the owner anything.
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
3. **Park, don't fix and don't ask.** Write one line: what it is, why it
   matters, where you saw it (file:line, run id, issue, message), and whether
   the owner must decide something. Then continue the main line. Do not open a
   ticket, start an agent or send a questionnaire for a parked item. If it is
   already parked (same issue or same title), add the new source to the
   existing line.
4. **Questions are for main-line decisions and urgent items only.** A question
   about a parked item waits for the review.
5. **End each reply with one next step on the main line**, then `side +N`, the
   number of items parked in this reply. Do not list parked items at the end
   of a reply.
6. **The owner's own new idea is not parked.** Do what they ask, and record the
   switch: pause the main line with the reason (`paused for: …`). When the
   detour is done, say so and resume the main line, or ask which line is now
   the main one if the detour grew.
7. **Review in batches.** When the owner asks to see the parked items ("review
   side lines", "看支线"), group them by area. For each group, write a short
   plan: what to do, in what order, and what the owner must decide. Do this
   before any fix. Ask the decisions together, then let the owner pick what
   becomes the next main line.
8. **Clear handled items.** An item is handled once it is filed as a ticket,
   scheduled at a time, done, or dropped by the owner; a filed ticket counts as
   handled by the owner. From then on the ticket or the scheduled item is its
   record, so remove it from the queue. With focus tools, close it with the
   ticket number, the scheduled item's id or the drop reason as the receipt;
   the tools keep the history and list only open items. In the local file,
   delete the line. Say in the reply what left the queue (`cleared: #N, #M`),
   so nothing disappears unseen.

## Done when

- The main line's done-when criterion is met, and the owner confirms or names
  the next main line.
- Every item reported during the work is either still open in the store or
  recorded elsewhere: a ticket, a scheduled item, or the owner's drop decision.
  Nothing exists only in chat, and nothing handled stays in the store.
