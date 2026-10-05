---
name: i-have-ocd
description: "Keep the current task focused, preserve consequential findings with an accountable owner, and ask the human only for unresolved decisions. Use it whenever the owner names a main line or says they are lost, scattered or pulled in too many directions, even if they only say 'I have OCD', 'keep me focused', 'one thing at a time', 'what are we doing again', '主线是', '我有点 lost', '分叉太多' or '先记下来'."
---

# I have OCD

Keep the current task moving. Preserve consequential findings without making
the human manage engineering ownership.

## Store and identity

Use scripts/ocd.py and its per-project SQLite store, shared by sessions and
worktrees on this machine. State its machine-local scope once. This release
implements only the local backend; a shared binding request is unavailable.
Never switch stores after an outage. Read the backend contract in
references/backend-contract.md for commands, schemas, migration and recovery.
The generated Markdown view is read-only and is never an input to normal reads.

The current task and its done-when criterion belong to this session. Use the
user's current request when it already establishes them; do not ask again just
because the store has no main-line row. Child agents return findings to their
parent and do not write this store, route work, or open a human review.

## Rules

1. Read this session's main line at start and after compaction. Before showing
   a decision, read its current state again. Do not replace another session's
   task with a project-wide main line.
2. Apply the intake gate before creating a concern: name an evidenced trigger,
   a concrete consequence, a source, and the responsible lane; check for an
   existing record that already owns it. A new concern requires both a concrete
   consequence and no owning record. If a record exists, link it and append
   evidence there within authorization; do not create a second work item.
   If no external record exists, the store's concern is the durable lane record
   and the discovering session remains accountable. A reminder with no applicable
   consequence does not enter the work or decision queue.
3. Main-line defects, blocking checks and already authorized work belong to the
   current execution record. The user's new request establishes or changes the
   task; it is not an unapproved side finding. Other consequential work goes to
   its owning lane. Routing does not require a human decision and does not grant
   permission for a new ticket, project, external write, or implementation.
4. Use a concern key made from the affected object, consequence and occurrence.
   Reuse the concern across sessions, handoffs and existing records. Add sources
   to it. A different consequence stays distinct. A new occurrence after a real
   resolution cites the old concern and the new evidence; wording changes and
   transfers are not recurrences. Uncertain similarity is not permission to merge.
5. Keep responsibility, delivery, human decisions and work completion separate.
   A route offer leaves responsibility with the sender until an acceptance
   receipt names the new owner and owning record. Delivery or an ACK is not
   acceptance. A human answer names one decision ID and its evidence; forwarding
   it links that answer instead of making another decision.
6. A pending human decision needs one unresolved choice, why existing authority
   is insufficient, feasible options with costs, a recommendation, and the
   consequence of not deciding. Do not ask the human who should handle ordinary
   engineering work. Open a review only when the human asks or a concrete
   main-line blocker requires it. Read current decisions and present all of
   them, across lanes, in that one review; when a question tool holds fewer per
   call, continue with further calls in the same turn. Answers, resume,
   compaction and lease expiry do not open another review.
7. Record 'later' with the original answer and a reopening condition: explicit
   human reopening, a named dependency completing, or new evidence changing the
   consequence. Silence is not 'later'. Time elapsed and repeated reads never
   reopen it. Keep responsibility and the unanswered choice visible in history.
8. Raise concrete money, production, credential or identity risks immediately
   in the next outward response or an already authorized incident channel.
   Distinguish confirmed facts from risks requiring verification. Record the
   first notification and current responder. Do not wait for an ordinary review,
   a routing receipt, a working backend, or a model. An urgent notification is
   not permission to perform the underlying action.
9. Keep routine replies about the main-line result and its next step. Do not
   print a side marker, global open count, routine routing list, or repeated
   backlog reminder. Surface a changed decision only when relevant to the
   current task or requested review; surface urgent risks immediately.
10. Every write has a request ID, and every update names the version read.
    Use fenced leases for exclusive work. A resolution needs evidence; an
    accepted handoff ends routing, not the work or an unanswered decision.
    Missing, locked, damaged, incompatible or unreachable state is unavailable,
    never an empty queue. Preserve undecidable cases for the accountable session;
    do not silently discard them or claim a model outage found no issues.
    Withdraw a question only with evidence that the choice no longer exists;
    withdrawal is not a human answer.

## Done when

The authorized task meets its done-when criterion. Every consequential finding
has a responsible owner and a durable record, or a supported disposition;
every real pending human decision remains discoverable. There is no requirement
to empty other lanes' work or ask for a new task before ending a completed task.
