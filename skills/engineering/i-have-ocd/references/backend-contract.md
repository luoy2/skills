# Backend contract

`i-have-ocd/1` is the public behavior contract. SQLite format 2 is its local
reference implementation. The protocol major and the SQLite format are different
numbers. The helper uses only the standard library and implements no shared
transport, network access, authentication service or message sender.

## Selecting a store

`--project`, then `I_HAVE_OCD_PROJECT`, then the repository's common Git directory
select the project. All worktrees of that repository use the same machine-local
store. Outside a repository the directory name is used. `I_HAVE_OCD_HOME` selects
the state directory; its default is the user's local state directory under
`i-have-ocd`. Runtime files never belong in the skill repository.

The store has immutable `store_id` and `workspace_id`, a revision, a backend
binding and a cutover epoch. An absent store is created only by intake, link,
main set or import apply. Reads never create a store. A missing, locked, damaged
or incompatible store returns `UNAVAILABLE`, never an empty successful queue.
`backend bind --kind shared` and `I_HAVE_OCD_BACKEND=shared` return
`UNAVAILABLE/SHARED_BACKEND_UNSUPPORTED` before reading or writing local data.
A stored shared binding also refuses local access. There is no fallback.

A future backend must negotiate this contract, intended workspace, supported
operations, identity requirements and review limit before use. The explicitly
selected backend remains selected after an outage. Multiple candidates or a
workspace mismatch cannot be resolved by guessing. This document specifies
semantics for that backend; this release does not implement one.

## Identity, transactions and errors

Use `--actor-id` or `I_HAVE_OCD_ACTOR_ID` for stable session identity. `--by` or
`I_HAVE_OCD_BY` is a display name. `--role child`, `I_HAVE_OCD_CHILD=1`, or a
non-null actor parent in the API envelope refuses every write with
`FORBIDDEN/RETURN_TO_PARENT`, including render, review and migration. A child
returns findings to its parent. Local role checks are cooperative checks between
processes belonging to one user, not a security sandbox. A future shared backend
must authenticate actor identity and workspace membership independently.

Each write needs a nonempty request ID. Its namespace includes store, workspace
and contract major. A canonical hash covers operation, actor, workspace,
expected versions, input and lease proofs. The result, event and state changes
commit in one transaction. Retrying the same ID and payload returns the original
receipt with `replayed=true`; a different payload or actor conflicts. Request
IDs are opaque strings. New entity IDs are UUIDs; migration accepts stable opaque
IDs supplied by the mapping. No read exposes a lease token or its hash. Only the
holder's acquisition result and identical acquisition retry return a token.
Export removes tokens from both data and embedded historical request results.

Each update compares the version read. API `expected` accepts exactly the keys
required by the operation. Creating main lines uses version 0; existing objects
cannot bypass comparison with 0. Normal reads and writes never migrate format 1.
All aggregate updates increment their own version; a lease has its own increasing
fence and does not change business state. Global revision advances with each
committed request event. A stale version returns current non-sensitive state.

Acquisition returns a random token and increasing fence. Exclusive writes
require holder, token, fence and an unexpired lease. Renewal and release check
the same proof. Expiry permits acquisition of a higher fence but never changes
ownership or decisions. Owner answers compare decision versions and do not
require the engineering lease. Merge checks both item versions and both leases
in one transaction. Responsibility transfers, merge links and request receipts
are atomic. SQLite backup is used for source snapshots; WAL is never ignored.

Success has `ok`, `contract`, `backend_id`, `store_id`, `workspace_id`, `revision`,
`request_id`, `replayed`, `result` and `receipt={event_ids,committed}`. Errors have
`ok=false`, `contract`, `error={code,reason,retryable}`, `committed`, and optional
`current`. Exit codes are 0 success, 1 `UNAVAILABLE`, 2 `INVALID`, 3 `CONFLICT`,
4 `FORBIDDEN`. Local failures before transaction commit report `committed=false`;
a failure after commit must report `committed=true`. A future transport with an
ambiguous result must use `committed=unknown` and recover using the original
request ID. It must never retry under a new ID or return an empty queue instead.

Item writes include `result.item_id/item_version/attention_state/needs_owner`
alongside the complete item. Decision and handoff writes also include their IDs
and versions. Review results include the fixed batch fields directly and under
`result.review`.

## Records and independent states

The store contains concerns, aliases, decisions, decision links, append-only
rulings, handoffs, leases, reviews, events, requests and actor-scoped main lines.
Entity data is stored as JSON with indexed identities; concern keys additionally
have a unique `(workspace_id, concern_key)` SQLite constraint. Aliases uniquely
map `(origin_store_id, legacy_id)` to concerns. Decision links uniquely map
`(concern_id, decision_id)`; a real shared choice uses one decision ID.

An intake supplies:

```json
{
  "key":{"object":"component:reader","consequence":"valid-records-rejected","occurrence":"release-1"},
  "summary":"Reader rejects valid input",
  "trigger":"The contract probe failed",
  "consequence":"Records cannot be imported",
  "sources":[{"ref":"probe-1","evidence":"Input and rejected output"}],
  "owner":"reader maintenance",
  "responsible_actor":"actor-a",
  "record_search":{"result":"none","checked":["current-task","record-index"],"evidence_ref":"search-1"}
}
```

The key is SHA-256 of sorted, compact UTF-8 JSON after Unicode NFC and surrounding
whitespace normalization of all three fields. No case folding, semantic rewriting
or similarity merging is performed. The key excludes current owner, session,
wording and delivery identity. A concurrent intake returns `created` once and
`existing` thereafter without changing responsibility. Closed and merged records
remain indexed; merged keys resolve to the canonical concern. New sources use a
versioned add-source operation, including on closed concerns.

A new concern requires a concrete consequence and documented search finding no
owning record. Existing records use `link` with `record_search.result=found` and
`owning_record={ref,owner,authority_ref}`. It returns `linked` or `existing`, not a
second triage item, and proposes no decision. A conflicting owning record is a
conflict, not an overwrite. Without an external record, the durable owning record
is `ocd:<store-id>/<concern-id>`. Intake responsibility must name the caller.
Unknown downstream ownership stays with the discovering session and can carry
`owner_resolution=needed`. Owners and record references are opaque; lanes are
not enumerated. `possible_duplicate_of` records uncertainty without merging.

A recurrence after a verified resolution uses a different occurrence key,
`recurrence_of` and new source evidence. Wording changes and transfers reuse the
original ID. A prior routing or scheduling receipt is not a verified fix.

- Routing is `owned`, `offered` or `accepted`. Offer keeps the sender responsible.
  Delivery receipts may be `queued`, `delivered` or `ack`, and never accept work.
  Only the named target can accept, with the offer version, owning record and
  `receipt={kind:accepted,ref,responsibility}`. Local delivery is always `manual`.
  There is at most one active offer per concern. Reject or cancel restores owned
  routing. Cancel requires the sender's lease. None of these operations answers
  or deletes a decision.
- Each decision has question, owner-only reason, authority reference, feasible
  options with costs, recommended option, inaction consequence, version, current
  ruling, reopen condition and optional supersedes. States are `owner_pending`,
  `owner_decided`, `later`, `withdrawn`. `needs_owner` is derived from pending
  decisions and cannot be written. A concern may have multiple independent
  decisions; each counts separately. `decision link` shares the original decision
  and ruling across concerns instead of duplicating an answer.
- An answer names one decision, option, answer reference and immutable ruling ID.
  `later` requires a real answer reference and condition
  `{kind:owner_request|dependency_completed|consequence_changed,ref,predicate}`.
  Reopen requires matching `event_kind`, `condition_ref`, `predicate`, new
  `event_ref`, `evidence_ref` and `incremental_question`. No day, timer, silence,
  repeated read, resume or lease expiry reopens a decision. Old rulings remain.
  Withdrawal requires the current responsible actor, evidence and reason plus
  `choice_no_longer_exists=true,basis=choice_removed`; it is not an owner answer.
- Lifecycle is `active`, `closed` or `merged`. Close requires a lease and
  `{kind:resolved|not_applicable|owner_dropped,ref,evidence}`. Pending or later
  decisions refuse closure. An outstanding offer must be settled first. Merge
  requires evidence, both leases and versions, and preserves all sources and
  decisions on the canonical item. The source and its history remain readable.
- Attention projects pending decisions first, then later, then decided, then
  routed for accepted or externally owned work, else triage. Closed and merged
  records show lifecycle. Accepted responsibility and a pending question may
  coexist; an answered decision does not imply completed engineering work.

Urgency records confirmed facts, pending verification, consequence, responder
and first actual notification separately. `urgent` with `action=raise` records
intent; `action=notified,notification_ref` records the first real outward receipt.
Later notifications do not overwrite it. The caller raises a concrete urgent
risk immediately even when storage is unavailable; this helper sends nothing.

## Reviews and main lines

Main lines are keyed by workspace and actor. The current request establishes
this session's task even if its row is absent. Another actor's task is not a
project-wide instruction. The old single main line is retained as legacy history
and is not broadcast to every actor.

`review start` requires `owner_scope` and
`trigger={kind:owner_request|main_line_blocker,ref}`. A fixed snapshot contains at
most four distinct decision IDs and their versions across all lanes in that
scope. Grouping does not reduce the count. The responsible parent must split
independent choices; a database cannot infer how many choices a sentence hides.
Only one review per owner scope is active, enforced inside the write transaction.
A simultaneous start returns that review. An expired presenter lease permits
acquisition of the same batch, not a new batch. Other actors receive no token.

Before presentation the backend rechecks each decision's state and snapshot
version, removes stale questions and does not refill. `present` and `finish`
require the review version and presenter lease. Presentation IDs accumulate
within the original four. Finishing may leave questions unanswered and does not
answer them. To request another batch, provide the previous review ID and a new
explicit trigger reference. Resume, answers, compaction, messages and expiry
never automatically produce another questionnaire. `has_more` is a continuation
hint for the presenter, not a global backlog broadcast.

List and history pages have at most 100 rows. Cursors bind an offset to a revision;
a changed revision returns `CURSOR_REVISION_CHANGED` so a caller restarts the
read rather than silently skipping records. Review batches have no automatic
pagination. Counts appear only under explicit `--include-counts`, and distinguish
work, pending owner decisions and later state.

## CLI and API

Run the adjacent `scripts/ocd.py`. JSON is default; `--text` pretty-prints it.
Common options are `--project`, `--actor-id`, `--by`, `--role`, `--text`.
`--data FILE` and `api --request FILE` accept `-` for stdin. Do not interpolate
complex payloads into shell commands. `--help` lists arguments for each command.
Every write below requires `--request-id R`.

| Command | Version and lease | Input |
| --- | --- | --- |
| capabilities | Read | Negotiated local capabilities and roles |
| intake / link --data FILE | Create | Intake shape above; link includes owning record |
| show ID --history --include-delivery | Read | Versions, decisions, optional history and handoffs |
| find --key HASH | Read | Canonical key lookup; API also accepts key object |
| list --owner TEXT --state STATE --cursor C --include-counts | Read | Filters lifecycle, attention or routing; default active |
| main get / main set | expected main for set | Set accepts title, done-when, next-step, paused-for, set-by, repeatable note |
| add-source ID --data FILE | expected item | sources array |
| take ID | expected item | Optional --ttl-minutes, default 60, range 1–1440 |
| renew / release ID | expected item, token/fence | Same lease proof; renew accepts TTL |
| route offer ID --data FILE | expected item, token/fence | target={actor_id,owner}, proposed_record, authorization_ref, instruction |
| route accept HANDOFF --data FILE | expected handoff | offer_version, owning_record, acceptance receipt |
| route receipt HANDOFF --data FILE | expected handoff | queued/delivered/ack receipt; never acceptance |
| route reject / cancel HANDOFF --data FILE | expected handoff; cancel token/fence | reason |
| decision propose ID --data FILE | expected item | Complete independent question |
| decision link ID --data FILE | expected item and --expected-decision-version | decision_id, evidence_ref |
| decision answer DECISION --data FILE | expected decision | option_id, answer_ref, ruling_id |
| decision later / reopen / withdraw DECISION --data FILE | expected decision | State-specific evidence described above |
| review start --data FILE | Create | owner_scope, trigger, optional previous_review_id |
| review show ID | Read | Fixed snapshot with current valid questions |
| review present / finish ID --data FILE | expected review, token/fence | presentation_ref and optional decision_ids / finish_ref |
| urgent ID --data FILE | expected item | raise or notified payload |
| close ID --data FILE | expected item, token/fence | Closure kind and evidence |
| merge FROM --into TO --data FILE | expected-from-version, expected-to-version; from/to-token and from/to-fence | evidence_ref, reason |
| history --after-event E --cursor C | Read | Bounded events |
| render | Explicit write | Generates Markdown; never absorbs edits |
| migrate plan / apply --source DB --mapping FILE | apply expected-revision | Explicit format upgrade |
| export --include-history | Read | Full portable snapshot, digest and legacy history |
| import plan / apply --data FILE | apply expected-revision | Validate or restore the snapshot |
| backend bind --kind shared | Unsupported | UNAVAILABLE; no transport or local fallback |
| api --request FILE | Same checks | Contract envelope below |

CLI versions use `--expected-version V`; lease proofs use `--token T --fence F`.
API versions use exactly `expected.item`, `decision`, `handoff`, `review`, `main`,
`revision` as appropriate. Merge uses `expected.from_item/to_item` and
`leases.from_item/to_item`. Intake, link, review start and render have empty
expected maps. A generic write envelope is:

```json
{
  "contract":"i-have-ocd/1",
  "workspace_id":"workspace-id",
  "op":"decision.propose",
  "request_id":"request-id",
  "actor":{"id":"actor-a","parent_id":null},
  "expected":{"item":7},
  "input":{
    "item_id":"concern-id",
    "question":"Extend format support?",
    "owner_only_reason":"Exceeds authorized scope",
    "authority_ref":"task-1",
    "options":[{"id":"keep","label":"Keep scope","cost":"No new support"},{"id":"extend","label":"Extend","cost":"More implementation and validation"}],
    "recommended":"keep",
    "inaction_consequence":"New format cannot enter this delivery"
  }
}
```

`park`, item-based `decide`, `done`, `drop`, `count`, and `import-md` return
`INVALID/CLIENT_UPGRADE_REQUIRED`. Old semantics are not guessed or translated.

API migration operations accept `input={mapping,source}` (source is optional),
with `expected.revision` for apply and an empty expected map for plan. They use
the same role, snapshot, version and request receipt checks as the CLI. Import
input is the exported `{snapshot,digest}` object and also uses expected revision
for apply.

## Format 1 migration

Stop old writers before an authorized cutover. Run `migrate plan --mapping FILE`
read-only first, optionally specifying `--source DB`. The default source is the
selected project's database. It uses a normal read-only connection plus SQLite
backup into a consistent snapshot; it never copies a live database file or uses
`immutable=1`. Plan writes only stdout. Start with `{"items":[]}` to obtain each
legacy ID, old version, raw row hash, source store ID, revision and Markdown diff.

The mapping is a JSON object with optional workspace ID, an `items` array and,
when the view diverged, an explicit `view_resolution`. Each item has:

- `origin_store_id`, `legacy_id`, `old_version`, `raw_hash` from the plan.
- `classification`: engineering, route_only, owner_pending, owner_decided, later,
  not_applicable, resolved or owner_dropped. These are explicit evidence-based
  mappings, not classifications inferred from the old writable bool.
- `concern_id`: stable UUID or opaque ID, and `concern`: full intake/link payload.
- `owner`, `responsible_actor`, `owning_record`: explicit responsible lane, actor
  and sole record reference. For an internal record, use the planned store/concern
  IDs to form its `ocd:` reference. A link additionally supplies record authority.
- `decision_ids`, `decisions`: the exact IDs and full independent questions. A
  question includes its ID, proposed state and, for decided/later, a `ruling`
  containing the original answer_ref, ruling_id, option_id or reopen_condition.
  Repeated canonical decision IDs link the same choice and ruling.
- `disposition`, `receipt_refs`, `uncertainty`: explanation, provenance references
  and unresolved gaps. Uncertainty must be an empty array before apply.
- Optional `handoff` names its ID, target actor and owner, proposed record,
  authorization and instruction. Without an acceptance receipt the sender
  remains responsible. An accepted mapping must include verified responsibility
  evidence and name the target as the responsible actor.
- A terminal mapping includes `closure={kind,ref,evidence}` matching its
  classification. Pending or later decisions cannot become terminal.

Route-only evidence creates a handoff and never an owner answer. A real question
creates owner_pending even if the old bool was false. An explicit answer links
its ruling. “Later” links the original answer and event-based condition, with no
day-based wake-up. No applicable consequence becomes not_applicable only with
supporting evidence. Incomplete evidence blocks apply. Old ticket, recorded,
transferred or scheduled receipts alone do not imply work resolution.

Every old ID must be mapped once, including historically closed rows. Different
legacy IDs may point to the same verified canonical concern; each alias remains.
An unabsorbed Markdown edit requires
`view_resolution={hash,evidence_ref,disposition}` and explicit concern mappings
for its consequences. A deleted line is never a closure instruction. The original
text and hashes remain in legacy history. New rows in the hand-edited text need
explicit `additional_concerns` mappings; they are never silently absorbed.

Apply uses `migrate apply --mapping FILE --expected-revision N --request-id R`.
It requires the source to be the selected target. A SQLite backup is retained
beside it. Under a write transaction it rechecks the source digest and view,
renames the raw format 1 tables to legacy tables, creates format 2, adds aliases
and mapping records, and commits a migration event and request receipt. Any
invalid mapping rolls back the entire schema change. The old main line remains
legacy; new actor tasks come from current requests. Old leases grant no format 2
rights. The old helper refuses format 2, and stale preloaded writers find their
old tables renamed. The backup contains the full raw original, including secrets
from old lease results, and must stay in controlled runtime storage.

An identical migration apply retry reads its committed receipt. Changed mappings
or revisions after cutover conflict. Never replace a failed mapping with guessed
responsibility or an invented answer just to reduce open counts.

## Export, import and projection recovery

`export --include-history` returns a complete format 2 snapshot and SHA-256 digest,
including aliases, decisions, links, rulings, reviews, receipts, legacy records
and request history. Tokens and token hashes are removed, including nested old
JSON results. Partial history exports are refused (`FULL_HISTORY_REQUIRED`): this
local release restores whole snapshots, not shared incremental batches.

`import plan --data FILE` validates the digest and relations without writing.
`import apply --data FILE --expected-revision N --request-id R` restores only into
an empty target at the expected revision. It preserves source identities and
history, appends an import receipt, increments the cutover epoch and fences every
imported lease. An active review keeps its fixed batch and can reacquire a
presenter lease. A retry is idempotent. A changed or nonempty target conflicts;
there is no silent merge with another store. No shared binding is implemented.

`render --request-id R` explicitly publishes a generated Markdown view. Normal
reads never write it; ordinary state changes may leave its last projection stale.
Hand edits or deletion after a render return `VIEW_DIVERGED` without committing
business changes or overwriting the file. Preserve the file for recovery. To
restore a projection, save the edited copy elsewhere, remove the view explicitly,
and rerun render with a request ID. Reads never interpret a missing line as a
resolution. SQLite remains authoritative even if rendering is interrupted.
