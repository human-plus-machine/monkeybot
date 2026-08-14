# Migrating pre-#179 conversation history to `agent_scope`

`conversation_history` (and, for Firestore, the `threads` summary collection)
is namespaced per agent as of PR #179, closing a leak where gateways sharing
one `db_url` could read and resume each other's threads. Rows/documents
written before that change carry no assignment to any agent. Migration
deliberately does not auto-claim them — see "Why there's no automatic
migration" below — so an operator who knows which legacy `thread_id` belongs
to which agent must assign it by hand before that thread becomes resumable
again.

## SQLite / Postgres

Per legacy `thread_id`, run directly against the database:

```sql
UPDATE conversation_history SET agent_scope = '<agent-id>'
WHERE thread_id = '<thread-id>' AND agent_scope = '';
```

Nothing else is needed: `list_threads`/`load`/`reset` all read the same
`agent_scope` column, keyed by the `thread_id` value, not by row id.

## Firestore

Two distinct pieces, not one field patch:

1. **`conversation_history` docs** (individual messages) — set `agent_scope`
   on the existing docs for the `thread_id`. These are looked up by the
   `thread_id`/`agent_scope` *fields*, so patching them in place is safe on
   its own.
2. **`threads` summary docs** (one per thread, for `list_threads`) — these
   are looked up by *document id*, computed by
   `monkeybot.core.persistence.firestore.firestore_summary_doc_id(agent_scope, thread_id)`.
   A legacy summary doc's id predates that scheme. Patching `agent_scope`
   onto it in place (leaving it at its old id) makes it satisfy the query
   filter but not the id lookup: the thread's next `append()` creates a
   *second*, correctly-keyed summary doc instead of updating the old one,
   and `reset()` only ever deletes the correctly-keyed doc — leaving the old
   one dangling with a stale count/preview, still surfaced by `list_threads`
   and selectable by `--continue`.

   **Delete** the old summary doc instead of patching it. A correct
   replacement is created automatically by the thread's next `append()`, or
   compute one immediately with `firestore_summary_doc_id()` (import it, or
   run `python -c "from monkeybot.core.persistence.firestore import
   firestore_summary_doc_id; print(firestore_summary_doc_id('<agent-id>',
   '<thread-id>'))"`) and write a new doc at that id if the thread needs to
   be listable before the next message.

## Why there's no automatic migration

An earlier version of this fix auto-claimed every legacy row for whichever
agent's gateway opened the database first after upgrading. Reviewed and
rejected: on a database genuinely shared by more than one agent, that agent
could read every other agent's pre-upgrade transcript, while the real
owners silently lost access — a deterministic cross-agent leak, not a rare
edge case (reproduced directly during review: opening as scope A on a
database holding both A's and B's legacy threads let A read B's secret and
permanently emptied B's own history). There is no way to infer the correct
owner of already-comingled, unlabeled history automatically; only an
operator with that knowledge can assign it correctly.
