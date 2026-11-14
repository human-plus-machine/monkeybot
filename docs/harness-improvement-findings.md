# Harness improvement findings

Review of the monkeybot harness at commit `ac086d85` on `develop` (core 3.14.1, cli 0.13.1), 10 Nov 2026.
Scope: performance, context management, prompt caching, and reliability. No code was changed for the review. Items 1, 2, 3, 4, and 5 are merged, including the store-parity case for item 4. C1, P1, P2, P3, P4, P5, C2, C3, C6, C7, C8, C9, C11, C12, C13, F5, F7, R2, R4, R5, R6, and R7 are merged. See the status notes under the summary.

Seven spikes were run by Grok 4.7 subagents to test the assumptions behind each finding. The spikes ran harness code against fake providers and in-memory or SQLite stores. No live model calls were made and no test suites were run.

Each finding carries one of three evidence labels:

- **Ran**: reproduced by running harness code in a spike.
- **Read**: taken from the source; not executed.
- **Inferred**: follows from the above but was not checked directly.

## Summary

The harness has a sound base: a stable/volatile system prompt split, tool-pair repair, pinned verdict rows, spill-to-file for large output, and atomic history rewrites on SQLite and Postgres. The gaps are at the edges of that design.

My ranking of what to fix first, by impact:

| # | Finding | Why it ranks here |
|---|---|---|
| 1 | No protection against context overflow (C4, C5). Merged. | An oversized request is sent as-is and the turn fails. Merged in [pull request 369](https://github.com/human-plus-machine/monkeybot/pull/369). See the status note. |
| 2 | No loop-level retry, no stall timeout (R1, R3). Merged. | One 429 on call 30 discards 29 rounds of work; a stalled Gemini stream never times out. Merged in [pull request 370](https://github.com/human-plus-machine/monkeybot/pull/370). See the status note. |
| 3 | Mid-request context updates reach the model once (C1, P2). Merged. | The model loses the updated todo list and the task anchor after one call; the same mechanism also breaks the cache prefix. Merged in [pull request 374](https://github.com/human-plus-machine/monkeybot/pull/374). See the status note. |
| 4 | History is loaded and token-counted twice per inner turn (F1, F2). Merged. | Up to about 1 s of blocking work per inner turn at 3,000 rows on the OpenAI path; two extra network round trips on Claude and Gemini. Merged in [pull request 371](https://github.com/human-plus-machine/monkeybot/pull/371). The store-parity case is merged in [pull request 372](https://github.com/human-plus-machine/monkeybot/pull/372). See the status note. |
| 5 | File tools block the event loop (F3, F4). Merged. | Reads now overlap, and a slow grep no longer stalls other sessions. F4 is merged in [pull request 368](https://github.com/human-plus-machine/monkeybot/pull/368). F3 is merged in [pull request 392](https://github.com/human-plus-machine/monkeybot/pull/392). See the status note. |
| 6 | Compaction fires at 70%, not 85% (C2). Merged. | The ratio trigger is 85%, and the background summary is cut so it still applies there. Merged in [pull request 379](https://github.com/human-plus-machine/monkeybot/pull/379). C3 is merged in [pull request 373](https://github.com/human-plus-machine/monkeybot/pull/373). |
| 7 | History rewrite drops row metadata (C6, C7). Merged. | C6 keeps `created_at`, `turn_id`, and `message_id` through a rewrite. Merged in [pull request 378](https://github.com/human-plus-machine/monkeybot/pull/378). C7 copies a dropped row into an archive before it leaves the live thread. Merged in [pull request 380](https://github.com/human-plus-machine/monkeybot/pull/380). An anchor on a summarized row still returns 409. |
| 8 | Cache prefix breaks (P1, P3, P4, P5). Merged. | P1 keeps the system text byte-identical across the calls of a request. Merged in [pull request 381](https://github.com/human-plus-machine/monkeybot/pull/381). P3 keeps that same system text across the next user message. Merged in [pull request 385](https://github.com/human-plus-machine/monkeybot/pull/385). P4 landed in [pull request 384](https://github.com/human-plus-machine/monkeybot/pull/384). An idle MCP server stays listed until the next history rewrite. P5 splits the Bedrock system cache point and sends an OpenAI cache key. Cost only; the dollar impact is not measured. |

The ranking is a judgment. Items 1 to 5 are ranked on behaviour I reproduced. Item 8 would move up if live usage shows poor cache hit rates on Anthropic or Bedrock, which I could not check.

**Merged.** F4 landed on `develop` in [pull request 368](https://github.com/human-plus-machine/monkeybot/pull/368) (`feat/file-tools-off-loop`). This note is not part of that pull request. `read_file`, `grep`, `glob`, `list_dir`, `load_file`, and the write tools (`write_file`, `replace_in_file`, `apply_patch`, including the before-image and the change record) run on a dedicated thread pool, so a batch of reads overlaps and a slow grep does not stall the loop. The pool is sized `min(32, effective_cpus + 4)` from the process affinity and any cgroup CPU quota. There is no environment variable. Grep starts ripgrep with `Popen` on that pool, and cancelling the call kills the child instead of falling through to the Python walker. Each read keeps the folder grants it started with. `palace.status()` and the gateway file-viewer routes also leave the loop.

**Merged.** F3 landed on `develop` in [pull request 392](https://github.com/human-plus-machine/monkeybot/pull/392) (`fde25ac7`, `perf/loop-stalls`). This note is not part of that pull request. The catalog seed counts stayed with `develop`: 468 rows, 462 filled, 487 citations. T2-CTX-23 was already listed. A thread only helps work that releases the GIL. tiktoken does, so the encode and the encoding lookup run in `asyncio.to_thread` for every tiktoken provider. At 850k tokens the longest loop gap during a count went from 162 ms to 3.1 ms, and at 189k tokens from 36 ms to 2.4 ms. AGENT.md and the skill folders are read in one thread hop at the start of `build_context`. History decoding, the request-size pass, and the Gemini conversion hold the GIL. A thread left a gap of about 12 ms, so they stay on the loop and hand it back on a 2 ms budget. `asyncio.sleep(0)` resumes the same task before timers that already came due, and the next chunk then blocks the loop again, so the yield is a zero-delay timer polled with IO. At 850k tokens (1,786 rows, the most a 1M window holds before compaction at 85%) the longest gap was 2.1 ms for a SQLite load, 2.3 ms for the size pass (22 ms before), and 2.6 ms for the Gemini conversion (21 ms before). The trim reuses that size instead of measuring again. The pass still does its work on every send. Left on the loop on purpose, all under 5 ms at 850k tokens: transform plus the snapshot copy (4.4 ms), compose, and the Claude and Bedrock conversions. Bedrock's conversion was 1.9 ms. One row larger than the budget cannot be split. The first Gemini conversion in a process pays the `google.genai` import, about 700 ms on this machine; the 21 ms figure is the conversion after that. The history was synthetic, about 2.7 KB per row. Postgres and Firestore got the same yield and were not timed. On this machine a 0.5 s sleep inside the Python walker left a longest event-loop gap of 11.5 ms, and the ripgrep path (two 0.5 s waits, still one after the other) left 11.6 ms. Before the F4 fix the same grep occupied the loop for the whole 0.5 s.

**Merged.** F1 and F2 landed on `develop` in [pull request 371](https://github.com/human-plus-machine/monkeybot/pull/371) (`830ecfe6`). This note is not part of that pull request. The store-parity case, T1-STORE-16, landed in [pull request 372](https://github.com/human-plus-machine/monkeybot/pull/372). This note is not part of that pull request either. F2 keeps the prompt size the provider just reported and estimates only the character change since that call, at 3 characters per token, with a band from half that estimate to three times it and 256 tokens of slack on either side. A real `count_input_tokens` runs on the first call of a request, after a history rewrite or a tool-list change, when the change is more than a quarter of the anchor, and when a 50%, 70%, or 85% bar sits inside the band. A report outside half to twice a real count, or outside the band of an estimate, drops the anchor. Gemini's prompt count is not added to its cache count. A missing usage report, a failed stream, or a cancelled stream does not become an anchor. The post-tool budget uses the anchor plus the reply's output tokens and reloads history only when a bar sits between those two numbers. With a provider that reports the true size of the request it sent, a 5-call chain over 20, 200, and 1,000 history rows did one preflight count. F1 keeps the first history load for the rest of the request and serves later loads from it after a last-row probe, so a write from outside the turn is not hidden. Covered by `tests/unit/core/runtime/test_prompt_ledger.py`, `test_prompt_ledger_loop.py`, and `test_request_history.py`. The 19 T2 context scenarios passed, and T3-CTX-01 skips `strict_script` because the cassette's own 8,107 tokens on a 10,000-token window compact on the tool turn.

**Merged.** R1 and R3 landed on `develop` in [pull request 370](https://github.com/human-plus-machine/monkeybot/pull/370) (`6db5bb40`, commit `10878f69`). This note is not part of that pull request. A transient failure is classified in `core/runtime/provider_retry.py` by walking the exception, its cause, and `.original`: statuses 408, 409, 425, 429, and any 5xx including Anthropic 529, plus connection and timeout errors. Any other exception ends the turn once, as before. When no assistant text from that call has reached the client, the loop sends the same request again, up to 5 times, waiting 2, 4, 8, 16, then 30 seconds plus jitter. A `Retry-After` header is kept and capped at 60 seconds. Stop and a steer interrupt the wait. A stall, meaning no stream event for 300 seconds, gets one retry. When assistant text has already been shown, the partial reply is stored, a finalized tool call is settled as interrupted, and the next inner turn continues it, at most twice per user message. `model.provider_max_retries` (default 5) and `model.stream_idle_timeout_s` (default 300) are hot settings; 0 disables each one. The Gemini client now has a 900 second total timeout, which also bounds `count_tokens` and the summarizer. Those calls do not get the idle timeout. There is no fallback model. Adapter retries still run first, so a hard outage can send the request up to 18 times. The client sees a thinking notice such as "Provider rate-limited. Retrying in 8 s (2 of 5)". The transcript records `provider_retry`, `provider_resume`, or `stream_stall`. The "provider stream done" log includes the longest gap between events. Covered by `tests/unit/core/runtime/test_provider_retry.py` and the idle cases in `test_preempt.py`. Pre-push local CI passed for `10878f69` (4412 tests, 57 skipped).

**Merged.** C1 and P2 landed on `develop` in [pull request 374](https://github.com/human-plus-machine/monkeybot/pull/374) (`958e8180`). This note is not part of that pull request. The system prompt stays the epoch baseline. Each later call of the request ends with a `## System context update` block containing only the volatile sections whose text still differs from that baseline: a changed todo list, the current-request anchor, a cleared section named as cleared. A section that returns to the baseline leaves the tail. The next user message, or compaction, opens a new epoch, folds the current text into the system prompt, and sends no tail. `SystemContextUpdated` still fires only on the call where a section changes. Anthropic `cache_control` and the Bedrock `cachePoint` sit on the block before the tail. The transcript stores the tail on the provider-request record, not inside a history message, and the reader shows it per call. Covered by `tests/unit/core/context/test_context_epoch.py`, `test_loop.py` (a 4-call todo change and the next user message), `test_anthropic_cache.py`, `test_bedrock_converse.py`, and T2-CTX-20. The SSE goldens `tool_turn.sse` and `cassette_turn.sse` carry the new wording. Re-running the epoch spike: calls 2, 3, and 4 each had the new todo list and `## Current request` in one tail block, the system hash was identical, and history did not store the tail. The cache spike puts the marker on the tool result before the tail. Its content-prefix percentage stays under 100% because that metric still counts the tail, which the next call does not resend in the same message. Not checked with a live Anthropic `cache_read_input_tokens`. The third call of a memory refresh, with the system text identical on all three, is covered in [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393). See that note.

**Merged.** C3 landed on `develop` in [pull request 373](https://github.com/human-plus-machine/monkeybot/pull/373) (`612a7a62`). This note is not part of that pull request. Read-time shaping takes its tier from `state.last_preflight_tokens`, the count of the request about to be sent. `usage.estimated_prompt_tokens` stays the peak for the turn and is still what gets stored. A compaction recount writes that count back onto `last_preflight_tokens`, so the same turn drops its tier. When a post-tool recount fails, the budgeter uses that count instead of the peak, so a failure after a compaction does not trim the new results and force another compaction. Covered by `tests/unit/core/runtime/test_pressure_tier_after_compaction.py`. The 19 T2 context scenarios passed with no scenario changes. `test_classify_status_and_sdk_errors` failed in the worktree with `ModuleNotFoundError: No module named 'openai'` because that extra is not installed there. Pre-push local CI passed for `08df49dc`.

**Merged.** C4 and C5 landed on `develop` in [pull request 369](https://github.com/human-plus-machine/monkeybot/pull/369) (`396d8924`). This note is not part of that pull request. Summaries and chat titles use the main model; `model.summarization_model` warns and is ignored. Each summary call stays within 85% of the window. A middle that fits is one call with the same prompt as before. A larger middle is folded, and a provider size rejection halves that call, up to three times. A size rejection of the main call compacts once and sends again. A second rejection, or a history with nothing left to summarize, returns "This request is too large for the model's context window. Send it in smaller pieces." The provider's text stays in the log. Only the newest row is kept when it is larger than the tail budget; an oversized head or second-newest row is summarized, and a tool response stays with its request. The harness still sends a request its own count says is over the window, and it does not trim a row mechanically. The runtime, config, persistence, and llm unit tests passed (876), and the 19 T2 context scenarios passed. In the C5 spike, a history at 250% of the window now takes 3 summary calls, the largest at 84% of the window. Before, it was one call at 231%. A size rejection whose summary then fails, and an inline summary that fails, are covered in [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393). See that note.

**Merged.** The counting half of C8 landed on `develop` in [pull request 376](https://github.com/human-plus-machine/monkeybot/pull/376) (`99eb1166`). This note is not part of that pull request. An image is counted at 2,000 tokens in the compaction tail, the tool-result budget, the prompt ledger, and the Bedrock and Vertex size fallback. That figure is the 1200 px preview cap on Anthropic, 43 by 43 patches, rounded up from 1,849. A file is still sized from its bytes. On this merge the base64 stayed in the stored row until the end-of-turn freeze. Converting the row to a reference when it is appended is in [pull request 397](https://github.com/human-plus-machine/monkeybot/pull/397). See that note. Covered by `tests/unit/core/runtime/test_history_compaction_split.py`, `test_context_budget.py`, `test_prompt_ledger.py`, `tests/unit/providers/test_bedrock_estimate.py`, and `test_bedrock_converse.py`. The 20 T2 context scenarios and the 7 attachment scenarios passed with no scenario changes. Pre-push local CI passed for `66d6367c`.

**Merged.** The reference half of C8 landed on `develop` in [pull request 397](https://github.com/human-plus-machine/monkeybot/pull/397) (`b5b69424`). This note is not part of that pull request. A `load_file` image or PDF is stored as an `AttachmentRef` when the tool row is appended, when a store is configured, the block has an `attachment_id`, and that id is in the store. Otherwise the base64 stays. Each later call of the request resolves the ref to the same preview. A request memo and the preview cache skip the disk read on a repeat. A missing tool file becomes a reload stub. A user-uploaded ref still raises. The end-of-turn freeze still replaces the ref with the same text stub. The voice loop is unchanged. An image ref counts as 2,000 tokens. A file ref counts the base64 length it replaced. T2-ATT-06 checks that the call after `load_file` still receives the image and the next user message does not. T2-ATT-02 checks the PDF. Pre-push local CI passed for `a5d04492`.

**Merged.** C9 landed on `develop` in [pull request 375](https://github.com/human-plus-machine/monkeybot/pull/375) (`67f6c65f`). This note is not part of that pull request. An unpaired tool result in the tail no longer decides the compaction cut, so the real request stays with its result and the provider is not shown empty arguments. Two adjacent assistant rows count as one turn, including when the cut falls between them. The orphan row stays in stored history. Covered by `tests/unit/core/runtime/test_history_compaction_split.py` (25 cases). The runtime and messages unit tests passed (486) before the split-turn follow-up, and the 19 existing T2 context scenarios passed with no scenario changes. A new T2 case was not added: the provider-request matcher records a tool request's id and name, not its arguments, and the in-memory repair puts that id back.

**Merged.** C2 landed on `develop` in [pull request 379](https://github.com/human-plus-machine/monkeybot/pull/379) (`15a90a89`). This note is not part of that pull request. The only token-ratio compaction trigger is 85%. The post-tool budgeter still compacts when the batch does not fit: headroom under twice the 2,000-token floor, or a result that was trimmed. A background summary started between 70% and 85% keeps a smaller tail, by the tokens still left before the bar, so rows appended in the band stay inside the tail it can apply. A jump past the 20% tail budget still falls back to an inline summary. Older tool results are reshaped only in the aggressive tier, which this path reaches when compaction cannot bring the prompt back under 85%. Replay answers a summary itself, so the call does not consume a cassette slot. T3-CTX-01 now expects 8 rows summarized: on its 10,000-token window the live count compacts when headroom is under twice the floor, later than the old 70% force. Covered by `tests/unit/core/runtime/test_context_budget.py`, `test_background_summary.py`, and `test_pressure_tier_after_compaction.py`. After rebasing onto `origin/develop`, those tests, the history-split tests, the 20 T2 context scenarios, T3-CTX-01, and the replay driver tests passed. Pre-push local CI passed for `a6ceec39`.

**Merged.** C6 landed on `develop` in [pull request 378](https://github.com/human-plus-machine/monkeybot/pull/378) (`fe80efdb`). This note is not part of that pull request. `history.reset` matches each survivor to its stored row by `row_id` and writes it back with that row's `created_at`, `turn_id`, and `message_id`. A new row, including the compaction summary, gets a stamp that keeps row order and no ids. A copy onto another thread does not take the source `message_id`, so the source stays idempotent. SQLite and Postgres stay one transaction. Firestore updates a matched document in place and writes new documents before it deletes anything, so a crash leaves extra old rows rather than a missing survivor. Re-running the Spike 5 reset script: the four rows kept their day-old stamp and ids through a reset and a compaction, retrying `message_id=m2` did not insert a duplicate, and `list_threads` stayed on the old timestamp. An anchor on the summarized row still returns 409 `ANCHOR_MISMATCH`. The dropped row is kept in the archive; see C7. Covered by `tests/unit/core/persistence/test_row_meta.py`, `test_history_row_ids.py`, and `T1-STORE-17` in `tests/contracts/test_storage_parity.py`. `T1-STORE-17` passed on SQLite, a local Postgres, and the Firestore emulator. `T2-STORE-01` passed with no scenario changes: it asserts `row_id`, which a rewrite already kept. On the emulator, `T1-STORE-05` still fails: a null `wing` is stored instead of rejected. That path was not changed. Pre-push local CI passed for `3ee65887`.

**Merged.** C7 landed on `develop` in [pull request 380](https://github.com/human-plus-machine/monkeybot/pull/380) (`3f4ce06b`). This note is not part of that pull request. Inline compaction, a background summary, and the load-max truncation copy the rows they drop into `conversation_history_archive` in the same write. Each archived row keeps its `row_id`, `created_at`, `turn_id`, and `message_id`, plus the rewrite id, the summary row that replaced it, and its old position. A later compaction archives the previous summary, so `load_archive` can walk the chain. A surviving row keeps the metadata C6 carries. A plain `reset` still deletes without archiving, and deleting the chat deletes the archive. SQLite and Postgres do the copy and the rewrite in one transaction. Firestore writes the archive documents first, with a document id derived from the live document, then runs the existing rewrite. There is no endpoint and no TTL. Rewind and edit into the summarized part still return 422 `SUMMARIZED`, and an anchor on a dropped row still returns 409 `ANCHOR_MISMATCH`. Covered by `tests/unit/core/runtime/test_history_archive.py` and `T1-STORE-18` in `tests/contracts/test_storage_parity.py`. `T1-STORE-17` stayed the C6 metadata case. The T2 store scenarios and the 20 T2 context scenarios passed with no scenario changes. `T1-STORE-18` ran on SQLite. Postgres and Firestore skip unless `MONKEYBOT_TEST_POSTGRES_URL` and `FIRESTORE_EMULATOR_HOST` are set. Pre-push local CI passed for `9a896ac7`.

**Merged.** P3 landed on `develop` in [pull request 385](https://github.com/human-plus-machine/monkeybot/pull/385) (`aa5695a0`). This note is not part of that pull request. The system text is the stable prefix plus `## Skills`. Current date, memory, the invoked skill, the todo list, background subagents, the current request, and PRE_TURN notes (git status, recall) are in the `## System context update` tail on every call, including the first call of a user message. A change in any of those no longer rewrites the prefix. Skills stay in the system text: the section is large and changes rarely, and a change still keeps the cache checkpoint before that heading. The "replaces the prior" and "Cleared" lines are gone, because the tail is the only copy the model is shown. The section-size report still sums to the snapshot; the live sections are counted inside the context-tail row. The verifier sticky nudge still appends `## Verifier` to the system text. Re-running the epoch spike against the worktree: the system hash stayed `e966a05a2b39` on every call of both user messages, and the todo section and runtime notes were absent from the system text. The cache spike's second-message case reported `system_equal: True`. A unit test changes the todo list, the git note, a memory drawer, and the date between two user messages and checks the system bytes, including the single Bedrock system block. T2-CTX-23 checks the todo and the git note. T2-CTX-22 stays the capped todo list. The memory and date variants are now Ran. Pre-push local CI passed for `f142e27c`. Not checked: whether a model follows a user-role tail as reliably as system text, and a live `cache_read_input_tokens`.

**Merged.** P1 landed on `develop` in [pull request 381](https://github.com/human-plus-machine/monkeybot/pull/381) (`382e3005`). This note is not part of that pull request. A one-shot note is the last section of the existing `## System context update` tail, under `## Runtime notes`, and it is sent on that call only. The system text is not changed. That covers a PRE_TOOL reminder, an empty-completion retry, a background-jobs nudge, a final-answer rejection, a doom-loop or replan note, and the evidence-guard correction. That correction used to be spliced onto the system message from `BEFORE_PROVIDER_REQUEST`. Request-long notes, such as the git status line, stay in the system text. The verifier sticky nudge still appends `## Verifier` there. Doom-loop and replan calls still empty the tool list, which is first in the cache order, so those calls lose the prefix for that reason. Re-running the epoch spike: the empty-completion case that went `bd22…`, `1693…`, `bd22…` stayed `bd22fcee0885` on all four calls. The note was on the user message of call 2 and was not stored in history. Covered by `tests/unit/core/runtime/test_loop_hooks.py`, `test_empty_completion.py`, `test_doom_loop.py`, `tests/unit/core/hooks/test_evidence_guard.py`, `tests/unit/providers/test_anthropic_cache.py`, `test_bedrock_converse.py`, and T2-CTX-21. The doom-loop, replan, todo-reminder, and evidence scenarios now look for the note in the messages. Git-context scenarios still expect `## Runtime notes` in the system text at this merge. P3 moves those notes into the tail. Pre-push local CI passed for `8e87819c` (4549 passed, 63 skipped). P3, in [pull request 385](https://github.com/human-plus-machine/monkeybot/pull/385), moves the request-long notes into the same tail. See that note.

**Merged.** C12 landed on `develop` in [pull request 377](https://github.com/human-plus-machine/monkeybot/pull/377) (`6c12e02d`). This note is not part of that pull request. PRE_TURN recall drops a drawer when a wake-up bullet equals its text, or when a 200-character cut bullet is a prefix of it. A miss leaves the drawer in. When every recalled drawer is already shown, the recall heading is left off, and the number skipped is logged as `memory_recall_skipped`. T2-MEM-01 now checks that turn 2's system prompt contains the fact. T2-MEM-10 checks the fact appears once and the recall heading is absent. Covered by `tests/unit/core/memory/test_recall_dedupe.py`. The memory, contract, t2, and t3 suites passed (661), apart from T3-PROV-06 and T3-PROV-07, which failed with `ModuleNotFoundError: No module named 'openai'` because that extra is not installed in the worktree. Pre-push local CI passed for `cd10a2d1`.

**Merged.** C11 landed on `develop` in [pull request 383](https://github.com/human-plus-machine/monkeybot/pull/383) (`ffb862b4`). This note is not part of that pull request. The todo list shown to the model stops at 6,000 characters: done items collapse to one line of ids, pending text is clipped to an equal share, and the in-progress item stays whole. A hand-edited list that is still too long drops lines from the end and says how many. The store still accepts 50 items of 500 characters, and a disk restore still does not apply those write caps. Background subagents list every running child and the first 8 finished ones, then a count, with a 2,000-character backstop. Runtime notes, including one-shot notes in the context tail, stop at 8,000 characters and keep the start and the end. The repo overview stops at 4,000 characters. Under each cap the text is unchanged. `SystemPromptSnapshot.sections` lists each section's on-wire characters, a characters-over-4 estimate, and the cap when one exists. The same numbers are on the debug log, and they stay on a transcript record whose prompt text did not change. Subagents no longer receive `ask_user` or `enable_loops`. `enable_mcp` stays. The parent tool list is unchanged. The harness sentence that names `enable_loops` is sent only when that tool is in the list. Covered by `tests/unit/core/prompts/test_prompt.py`, `test_section_budget.py`, `tests/unit/core/runtime/test_prompt_sections.py`, and T2-CTX-22. Pre-push local CI passed for `6a9e97f6`.

**Merged.** C13 landed on `develop` in [pull request 382](https://github.com/human-plus-machine/monkeybot/pull/382) (`5c21958e`). This note is not part of that pull request. A subagent result keeps `final_message` and no longer also sends that string as `assistant_text`. The last 10 tool snippets, 600 characters each, stay. A second `read_file` of the same thread, path, and line range is stored as a short stub only when the earlier result is still in that thread and its text still matches. The ledger is a hint. The stored row is the check. A missing row, a trimmed row, another thread, a cleared ledger, or a result under 2,000 characters is stored in full, and the first read of a range does not load history to decide. The read after a stub is sent in full, so one extra call gets the text back. Compaction still clears the ledger, which drops the hint with the read stamps. The live `ToolCallResult` still carries the full text. The voice loop is not covered. Covered by `tests/unit/core/runtime/test_read_dedupe.py` and T2-FILE-38. T2-FILE-38 checks that the request after the second read holds the file text once and the stub once. Re-ran that scenario after rebasing onto `382e3005`. Pre-push local CI passed for `25c1353b`.

**Merged.** R6 landed on `develop` in [pull request 386](https://github.com/human-plus-machine/monkeybot/pull/386) (`9589c33a`). This note is not part of that pull request. A cycle of the same two or three calls, each with the same arguments, trips doom-loop recovery after `DOOM_LOOP_THRESHOLD` repeats. The default is 3, so an A-B cycle stops on the 6th call and an A-B-C cycle on the 9th. Recovery is the existing path: an error, a harness note, and the next provider call with no tools. A call with different arguments breaks the cycle. A run of one distinct call still uses the original identical-call message. Exempt tools stay invisible to the cycle, and `DOOM_LOOP_THRESHOLD=0` disables it. A read followed by a failing edit whose guess changes each time still does not trip: those calls are not the same cycle, and the successful read resets the path-failure streak. Covered by `tests/unit/core/runtime/test_doom_loop.py` and T2-LOOP-49 (`regression/t2/loop/scenarios/loop/doom_alternating_calls.yaml`). The six T2 doom scenarios passed. The catalog census moved from 466 to 468: `develop` already listed T2-MCP-27, and this adds T2-LOOP-49. Pre-push local CI passed for `d623c868` (4606 passed, 63 skipped).

**Merged.** F7 landed on `develop` in [pull request 389](https://github.com/human-plus-machine/monkeybot/pull/389) (`5f0d9c18`). This note is not part of that pull request. A timeout or a connection error to the secret-scan bridge opens a breaker for that URL and token for 30 s. Later scans in the window are not sent and are not remembered as clean. An HTTP status does not open it, a text too large to send is still withheld, and a new token is scanned at once. A hung bridge took 200 s for 40 scans before the fix and 5.0 s after: one 5 s timeout, then the rest of the window skips. The hook caps were timed and left alone. See the F7 section. The catalog seed now counts `T2-MCP-27`: 467 rows, 461 filled, 486 tests. Pre-push local CI passed for `8dcce16b`.

**Merged.** R7 landed on `develop` in [pull request 391](https://github.com/human-plus-machine/monkeybot/pull/391) (`60f2855f`). This note is not part of that pull request. On the SSE gateway, with the sandbox off, a local background command is owned by the session, not by the request that started it. Ending the turn does not kill it, and Stop does not either. `kill_command` does, and closing the session does. The cap of 4 and the 3300 second ceiling stay, and the cap is now per session. When a command finishes and the model has not awaited it, the session starts a harness turn, the same way a background subagent does. Those wakes share one cap of three in a row, and a human message resets it. The prompt lists running and finished commands under `## Background commands`, in the context tail. The `run_command` text for these sessions says the job keeps running and that a harness note wakes the model. Sandbox commands, voice, and Lambda still kill at turn end, and the loop-driver scenarios still cover that path: T2-SHELL-14, T2-LOOP-16, and T2-LOOP-18 passed with no scenario changes. Covered by `tests/unit/core/tools/test_session_jobs.py` and `tests/unit/gateway/sse/test_job_wake_drain.py`. The catalog census is 467 rows, 461 filled, and 486 tests, including T2-CTX-23, which was already listed. Pre-push local CI passed for `6f0fd9bc`.

**Merged.** P5 landed on `develop` in [pull request 390](https://github.com/human-plus-machine/monkeybot/pull/390) (`c69a5248`). This note is not part of that pull request. Bedrock Converse sends the stable system text, then the cache point, then the volatile tail (`## Skills`, and `## Verifier` when the sticky nudge appends it). A change in that tail no longer drops the system checkpoint. A prompt with no volatile heading stays one block, and a cache-point rejection retries with both text blocks and no points. OpenAI sends the session id as `prompt_cache_key` unless retention is `none`, and drops that field or `prompt_cache_retention` after a 400. Gemini stays on implicit caching. The scorecard has a cache-read share row, and the provider-stream log includes cache read and creation. The catalog census is 467 rows, 461 filled, and 486 citations: T2-CTX-23 was already listed. Pre-push local CI passed for `063a2452`. Not checked: a live Bedrock `cacheReadInputTokens` after a skills change, and the dollar impact.

**Merged.** R2 landed on `develop` in [pull request 387](https://github.com/human-plus-machine/monkeybot/pull/387) (`d8e5e667`). This note is not part of that pull request. A failed `count_input_tokens` no longer ends the turn. The preflight catches the error and uses a local estimate, both before the model call and on the recount after compaction. With a trusted anchor and the same tool list, the estimate is the anchor plus the character change, at 3 characters per token, even when the change is large or a pressure bar sits in the band. Otherwise the whole request is sized at 3 characters per token. A usage report within half to twice that guess becomes the anchor, so later calls skip the count. A report outside that range drops the anchor and the next call counts again. The guess is not recorded as a real count. No new T2 case: the scripted scenarios do not fail `count_input_tokens`. Covered by `tests/unit/core/runtime/test_preflight_count_failure.py` and `test_prompt_ledger.py`. The runtime unit tests passed (509), and the 23 T2 context scenarios passed with no scenario changes. `test_catalog_pairing` failed on `develop` because the catalog lists T2-MCP-27 and the census still said 466 rows, 460 filled, and 485 tests. The census in this pull request is 467, 461, and 486. Pre-push local CI passed for `aeba263c`.

**Merged.** The section 9 gaps for a failed summary and the third memory call landed on `develop` in [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393) (`4a068670`, `test/section9-gaps`). This note is not part of that pull request. A provider size rejection whose summary raises returns the fixed overflow error and does not rewrite history. A summary that is also too long is halved three times, then the same error. An inline summary that fails still answers the turn, reports `turns_summarized=0`, and leaves the existing rows in place. `test_loop_picks_up_refreshed_memory_between_turns` runs three calls and checks that the system text is identical on all three. Covered by `tests/unit/core/runtime/test_context_overflow_loop.py`, `test_summary_fold.py`, T2-CTX-24 (`overflow_recovery`), and T2-CTX-25 (`overflow_summary_fails`). The catalog seed is 470 rows, 464 filled, and 489 tests: develop's 468 plus these two scenarios. The focused set passed (20) after the merge onto develop. The multi-turn failure spiral in section 8 is still open.

**Merged.** R4 and R5 landed on `develop` in [pull request 396](https://github.com/human-plus-machine/monkeybot/pull/396) (`ed300748`). This note is not part of that pull request. A text-only answer with `Done(truncated=True)` is stored and continued while two continuations remain and the turn limit allows another call. The segment is persisted, `RESUME_NOTE` is queued, and the client sees "The response was cut off. Continuing." The next call's answer is joined to the earlier segments with a newline for the final-answer gate and `turn_output_text`. Each segment stays its own assistant row. After the budget, or when the turn limit blocks another call, the reply is delivered and an `inlineMessage` says "This answer was cut off at the model's output limit." That notice is not stored in history. A thinking-only truncation still uses the empty-completion retry. Truncated tool batches are unchanged. The SSE `Error` frame no longer carries `str(exc)`. `client_error_text` names the failure kind, and appends the HTTP status and request id when those are known. A context overflow uses `CONTEXT_OVERFLOW_ERROR`. `ProviderRateLimitError` and `ProviderServerError` pass through. The log, the transcript, and the provider hook keep the raw text. The realtime websocket, the subagent worker, and the config-reload admin error still send `str(exc)`. Covered by `tests/unit/core/runtime/test_truncated_text.py`, `test_error_text.py`, `test_provider_retry.py`, T2-LOOP-50, T2-LOOP-51, T2-LOOP-37, and `test_setup_failure_releases_lock_and_keeps_prompt`. The catalog seed is 473 rows, 467 filled, and 492 tests, after T1-GW-07 from pull request 395. Pre-push local CI passed for `e5878417`.

## 1. Assumptions in the framing

These are the assumptions built into the question and into how I approached it.

| Assumption | Status |
|---|---|
| "Performance" means latency and token cost per turn, not throughput of the gateway. | Not confirmed with you. I covered both where the same cause affects both. |
| The main workload is long tool-using coding turns, so per-inner-turn overhead matters more than per-message setup. | Not confirmed. The recorded eval runs are short (about 2.7 tool calls per scenario). |
| One gateway process serves several sessions at once, so blocking the event loop affects other users. | Read from the gateway code. Deployment shape not confirmed. |
| Anthropic-style prefix caching is the cache model that matters most. | Not confirmed. The only recorded usage data is from `glm-5.3-flash` on `ollama-cloud`. |
| The loop is the only writer to a thread's history during a request. | Traced, and false for one writer. A voice call appends to the same thread without the turn lock. Rewind, edit, purge, and scheduler ticks take that lock. Subagents write a `subagent:` thread. See F1. |
| Token counts measured with `cl100k_base` are close enough to each provider's real tokenizer. | Proxy only. Expect a difference of some percent per provider. |
| Spike timings on this laptop with synthetic histories reflect production. | They show scaling, not absolute production numbers. |

## 2. What is known for certain

These are the facts the findings are rebuilt from.

1. The model knows only what is in the request it receives. Anything it must keep knowing has to be in every request or in stored history.
2. Provider prompt caches match on a byte prefix in the order tools, system, messages. A change at any point discards everything after it.
3. Every provider call returns the exact input token count for the request just sent.
4. The context window is a hard limit. A request over it is rejected by the provider.
5. One asyncio event loop runs all sessions in a gateway process. Synchronous work on it delays every session.
6. Network calls to providers fail transiently (429, 5xx, dropped streams) and sometimes stall without failing.
7. `conversation_history` rows are the only durable record of a thread. The NDJSON transcript is off by default.

What the code does per inner turn, measured in Spike 3:

| Work | Quiet tool turn | Turn in the 70–85% band |
|---|---|---|
| Full `history.load` | 2 | 6 |
| `count_input_tokens` | 2 | 3 |
| `history.reset` | 0 | 1 |
| `transform_context` | 2 | not broken out |
| `convert_to_provider` | 2 | not broken out |
| `compose_stable_baseline` | 2 | not broken out |
| AGENTS.md re-read | 1 | not broken out |
| Memory wake-up (when configured) | 1 | not broken out |

Fixed prompt cost for the t4 agent, measured in Spike 4 (`cl100k_base`):

| Part | Tokens |
|---|---|
| AGENT.md | 1,035 |
| Harness fixed context (includes 437 for the coding protocol) | 1,574 |
| Repo overview | 83 |
| Volatile tail | about 94 |
| **System prompt total** | **2,783** |
| 21 tool schemas | 3,631 |
| **Total** | **about 6,414** |

The same count through the OpenAI wrapper is 7,170. The five largest tool schemas are `grep` 519, `task` 429, `run_command` 402, `todo_list` 314, and `read_file` 284. A subagent gets the same system prompt and 17 tools, 5,460 tokens in total.

Recorded live usage, from the 24 run files in `regression/t4/runs/` (27 Oct 2026, `glm-5.3-flash` on `ollama-cloud`, 100 scenarios, 271 tool calls):

| Tool calls in scenario | Scenarios | Uncached prompt tokens | Cache-read tokens | Cache-read share |
|---|---|---|---|---|
| 1–2 | 66 | 623,610 | 762,240 | 55.0% |
| 3–5 | 28 | 511,014 | 549,184 | 51.8% |
| 6+ | 6 | 169,492 | 390,720 | 69.7% |
| **All** | **100** | **1,304,116** | **1,702,144** | **56.6%** |

Two limits on this data. The scenarios are short, about 30k prompt tokens each, so most of each prompt is the fixed 7k prefix. And no scenario triggered a summarization (`summarizations_count` is 0 in all 100), so compaction has no live-eval coverage in these runs. The data shows caching works on that provider; it does not show which of the cache findings below cost how much.

## 3. Context management

### C1. Mid-request context updates reach the model on one call only (Ran). Merged.

When volatile state changes inside a request (a todo update, the current-request anchor), the tracker sends a one-off `## System context update` block folded into the last user message of the next call. It is not stored and not repeated.

In a 4-call run the system prompt hash was identical on all four calls and showed the todo list from the start of the request. Call 2 alone carried the update. Calls 3 and 4 had neither the new todo list nor the `## Current request` anchor. The anchor exists to restate the task during long tool chains, and it is visible on exactly one call.

- The tracker advances only its fingerprints on a change (`core/context/epoch.py`); `baseline_volatile` is never updated, so the system prompt keeps the old state.
- The update wording names only "current-date, memory, skills, and current-request". Todos, invoked skill, and background tasks are sent but not named.
- No test asserts what the model sees on the call after the update. `test_loop_picks_up_refreshed_memory_between_turns` stops at two calls.
- The subagent worker has the same behaviour. The realtime loop does not use the tracker.

Direction: keep each update attached to the message it was first sent with for the rest of the request. From facts 1 and 2, this makes the update visible on every later call and keeps the request append-only, which also fixes P2. Not tested.

Merged in [pull request 374](https://github.com/human-plus-machine/monkeybot/pull/374), with a different shape from that direction. The tail is rendered again on every call from the sections that differ from the baseline, rather than left pinned inside the message that first carried it. See the status note. The finding text above is the review, not the current code.

### C2. Compaction fires at 70%, not at the 85% the constants suggest (Ran, Read). Merged.

`SUMMARY_TRIGGER_RATIO` is 0.85, but in the moderate tier (70% and above) `ContextBudgeter.fit_content_blocks` forces compaction:

```python
needs_compaction = remaining < self.floor_tokens * 2
if self.pressure_tier in ("moderate", "aggressive"):
    needs_compaction = True
```

So the first tool batch at or above 70% compacts inline. Spike 3 saw 76% drop to 25% with 29 rows summarized and 6 history loads in that turn.

Two consequences:

- The background summary (start at 70%, apply at 85%) gives no latency benefit on tool turns. `take` awaits the job as soon as the forced compaction runs.
- Entering the moderate tier reshapes all older tool results once (Spike 2: 11.97% of the cached prefix kept), and the compaction that follows resets the prefix anyway. The reshape is wasted work.

Direction: pick one trigger. Either stop forcing compaction in the moderate tier so the background job has its window, or remove the background machinery and compact at 70% on purpose. Skip the read-time reshape when compaction is about to run.

Merged in [pull request 379](https://github.com/human-plus-machine/monkeybot/pull/379), taking the first of those. The ratio trigger is only 85%. The background job keeps a smaller tail so it still applies at that bar, and older rows are reshaped only in the aggressive tier. See the status note. The finding text above is the review, not the current code.

### C3. The pressure tier stays high after a mid-request compaction (Ran, Read). Merged.

The tier used for read-time shaping comes from `usage.estimated_prompt_tokens`, which is a per-request high-water mark (`turn_loop.py` `_apply_pressure_and_before_provider`, l.990). After compaction brings usage to about 25%, the tier stays "moderate" until the next user message, so older tool results keep being shaped for no reason.

Direction: derive the tier from the most recent count, and reset it after a compaction. Merged in [pull request 373](https://github.com/human-plus-machine/monkeybot/pull/373). See the status note. The finding text above is the review, not the current code.

### C4. Nothing protects against context overflow (Ran). Merged.

- No handler exists for a provider "context length exceeded" error.
- No check refuses or repairs a request whose preflight count is over the window. In the spike a preflight of 11,611 tokens against an 800-token window was sent.
- The only reaction is a warning logged after the call, when the provider-reported prompt size is above the configured window (`turn_loop.py` l.1277).
- A summarizer failure is swallowed (`turn_loop.py` l.861–870) and the oversized main call goes out.
- `HISTORY_LOAD_MAX` (5,000 rows) did not fire in any of these cases.
- The tail keeps at least 2 rows whatever their size. A tail of 200% of the window was observed, and nothing reduces it.

Direction, from fact 4: add one guaranteed path before each send. If the count is over the window, compact; if still over, trim the largest rows in the tail; if still over, return a clear error to the user. Treat a provider context-length error the same way, once.

### C5. The summarizer prompt has no size cap (Ran). Merged.

`_generate_summary_text` (`history_compaction.py` l.682) builds one prompt from the whole middle. It is sized against the main model's window, not the summarizer's. A 65% middle of a 200k window is about 130k tokens, which is 102% of a 128k summarizer model. Tool results are capped at about 16k characters each; plain text rows are not capped. `CONTEXT_SUMMARIZATION_MODEL` changes only the model id.

A failure spiral (failed summary, larger history, larger summary prompt next time) follows from C4 and C5 together. Inferred, not reproduced.

Direction: cap the summarizer input to the summarizer model's window, and summarize the middle in chunks when it does not fit.

### C6. `history.reset` drops row metadata (Ran; Firestore part Read). Merged.

Compaction, attachment freeze, and truncation all call `reset`, which deletes every row and re-inserts the survivors (`persistence/history.py` l.346). `row_id` is kept. `created_at`, `turn_id`, and `message_id` are not.

- The thread jumps to "newest" in `list_threads`.
- `message_id` idempotency is lost. A retried append inserted a duplicate row while the memory outbox row remained.
- Rewind, edit, and fork anchors for dropped rows return 409 `ANCHOR_MISMATCH`.
- SQLite and Postgres resets are atomic. The Firestore reset is not: it deletes in batches of 400 and then appends row by row, so a crash can leave the thread empty or partial.

Direction: carry `created_at`, `turn_id`, and `message_id` through the rewrite. On Firestore, write the new rows before deleting the old ones, or switch generations with a marker. Merged in [pull request 378](https://github.com/human-plus-machine/monkeybot/pull/378). See the status note. The finding text above is the review, not the current code.

### C7. Compacted history cannot be recovered (Ran). Merged.

Rows removed by compaction are deleted from `conversation_history`. Earlier summaries are summarized again and then deleted. The NDJSON transcript is off unless `runtime.transcript_enabled` is set, and it is not a row archive.

Direction, from fact 7: archive dropped rows before deleting them. This also gives rewind something to restore from. Merged in [pull request 380](https://github.com/human-plus-machine/monkeybot/pull/380). See the status note. The finding text above is the review, not the current code. Rewind into the summarized part is still refused.

### C8. Tool-loaded images are counted as base64 text (Ran). Merged.

Images returned by `load_file` stay as base64 in history until the end-of-turn freeze. The estimator counts base64 characters divided by 4, so a 1 MiB image counts as about 349,528 tokens, against 3 for an `AttachmentRef`. `fit_content_blocks` does not trim images. Within that turn this distorts the tail split and the compaction budget.

User uploads are not affected; they are stored as `AttachmentRef` from the start.

Direction: count image blocks with a fixed per-image estimate, and convert to a reference when the row is appended. The estimate landed in [pull request 376](https://github.com/human-plus-machine/monkeybot/pull/376). The reference landed in [pull request 397](https://github.com/human-plus-machine/monkeybot/pull/397). See the status notes. The sentences above are the review, not the current code. On `develop` an image counts as 2,000 tokens, and a `load_file` image or PDF with an `attachment_id` in the store is an `AttachmentRef` from the moment the tool row is appended.

### C9. An orphan tool response in the tail drops a real request (Ran). Merged.

If the tail contains a tool response with no matching request, `_extend_tail_to_keep_tool_pairs` gives up, and a genuine request is dropped. `transform_context` then invents a `ToolRequest` with empty arguments to repair the pair.

This is an edge case. Well-formed histories gave 0 violations in 122 random compactable cases, and pinned verdict rows survived storage in 68 of 68.

Direction: drop the orphan and continue pairing. Merged in [pull request 375](https://github.com/human-plus-machine/monkeybot/pull/375). See the status note. The finding text above is the review, not the current code.

### C10. Thresholds are fractions only, with no absolute ceiling (Read)

All thresholds are fractions of `model.context_window`, default 200,000. A 1M-token model compacts at an effective 700k, which is slow and expensive per call long before that. `model_capabilities.py` has no window table, so the window is whatever is configured. The background `_pending` dict is per process, so other workers and restarts fall back to inline compaction.

Direction: allow an absolute token ceiling alongside the fraction.

### C11. Some prompt sections have no cap, and nothing reports per-section cost (Ran). Merged.

The fixed cost (about 6.4k tokens) is modest. The gaps are:

- The todo list is uncapped: worst case 25,840 characters, 3,549 tokens.
- Background tasks, runtime notes, and repo overview characters are uncapped.
- Subagents still receive `ask_user`, `enable_loops`, and `enable_mcp`. Worth reviewing whether each is usable there.
- Only the total is reported (`ContextUsage`) plus the raw text (`SystemPromptSnapshot`). No per-section token count exists, so growth in one section is invisible.

Direction: cap the uncapped sections and record tokens per section. Merged in [pull request 383](https://github.com/human-plus-machine/monkeybot/pull/383). See the status note. The finding text above is the review, not the current code.

### C12. The same memory can appear twice (Ran, Read). Merged.

In thread-scoped mode a drawer can appear in both the wake-up block (15 drawers of 200 characters, about 770 tokens at 100 or more drawers) and the PRE_TURN recall (up to 2,000 characters, about 462 tokens). Both can repeat text that is still in live history. The default is shared mode, where wake-up is the top 15 across the wing.

Direction: remove drawers from recall that wake-up already included. Merged in [pull request 377](https://github.com/human-plus-machine/monkeybot/pull/377). See the status note. The finding text above is the review, not the current code.

### C13. Repeated reads and subagent results carry duplicate text (Read). Merged.

- Reading an unchanged file again returns the full content again. `FileReadLedger` guards edits only.
- A subagent result holds the final text twice, as `final_message` and `assistant_text` (`core_tool_executor.py` l.564–572), plus the last 10 tool results at 600 characters each. The final text has no cap other than generic spill.

Direction: send the subagent's final text once. For repeated reads, return a short "unchanged" stub, and clear the ledger on compaction so the stub never points at content that was removed.

Merged in [pull request 382](https://github.com/human-plus-machine/monkeybot/pull/382). The stub is stored only after the earlier result is confirmed still in that thread. Clearing the ledger is not the only guard: a missing or trimmed row, another thread, and a restart also send the file again. See the status note. The finding text above is the review, not the current code.

## 4. Prompt cache

Cache order is tools, then system, then messages. The system prompt is split at the first volatile heading: the stable block is marked for caching and the volatile block is not. The last block of the last two messages is also marked.

### P1. One-shot runtime notes change the system text for a single call (Ran). Merged.

A one-shot note is appended to the system text for one call and removed on the next. Spikes 1 and 2 saw the hash go `bd22…`, `1693…`, `bd22…`. Nothing after the stable system block is reusable on the way in or on the way out.

Triggers: empty-completion retry, background-jobs nudge, final-answer gate, PRE_TOOL notes, and doom-loop or replan notes.

Direction: deliver one-shot notes as a message at the end of the request, not as system text.

Merged in [pull request 381](https://github.com/human-plus-machine/monkeybot/pull/381). The note is the last section of the existing tail, not a separate message, so the cache marker, the transcript, and the tail splitter already skip it. Re-running the epoch spike, the empty-completion hash stayed `bd22fcee0885` on all four calls. At that merge, PRE_TURN notes stayed in the system text. P3 moves them into the tail. The verifier sticky nudge still appends `## Verifier` to the system text. See the status note. The finding text above is the review, not the current code.

### P2. Removing the update block breaks the message prefix (Ran). Merged.

The mid-request update from C1 is removed on the following call, which changes the content of an earlier message (95.0% and 94.54% of the prefix kept in two runs). This happens on call 2 of nearly every tool-using request, because of the current-request anchor, and on every todo change.

Direction: the same fix as C1.

Merged with C1 in [pull request 374](https://github.com/human-plus-machine/monkeybot/pull/374). The tail is not removed from an earlier message, because it is not stored there. The cache marker is on the block before it. See the status note. The finding text above is the review, not the current code.

### P3. The first call of the next user message often has a different system text (Ran for todos and PRE_TURN notes; Inferred for memory and date). Merged.

If volatile state changed during or after the previous request, the system text differs, so the whole conversation after the stable block is written to the cache again. Spike 1 saw the divergence at character 6,464 inside `## Todo list`; git-status notes diverge at `## Runtime notes`.

When nothing changed, Spike 2 measured a byte-identical system prompt and a 100% content prefix.

Direction: move volatile content out of the system prompt and into the end of the messages, so system plus history stays a stable prefix. Models may weight system text differently from message text, so this needs a behaviour check.

Merged in [pull request 385](https://github.com/human-plus-machine/monkeybot/pull/385). The tail carries every non-empty live section on every call, and PRE_TURN notes ride it too. Skills stay in the system text. Re-running the epoch spike, the system hash was `e966a05a2b39` on both user messages, with no todo section and no runtime notes in the system text. A unit test now runs the memory and date variants, so those are Ran. The behaviour check with a live model is still open. See the status note. The finding text above is the review, not the current code.

### P4. Any change to the tool list invalidates everything (Ran). Merged.

A tool-list change kept 1.84% of the prefix. Triggers: `enable_mcp` and `disable_mcp`, MCP idle hiding after 10 unused inner turns, `enable_loops`, and TOOL_DEFINITION hooks.

Tool order is deterministic. `input_schema` key order is passed through from MCP servers without canonicalizing, so a server that reorders keys between connections breaks the cache.

Direction: avoid changing the tool list mid-thread where the saving is small. Idle hiding saves schema tokens but costs a full cache rewrite. Sort schema keys.

Merged in [pull request 384](https://github.com/human-plus-machine/monkeybot/pull/384) (`40858ba5`). A server that has been idle for 10 inner turns stays listed until the next history rewrite, and the note is attached to the following tool result. `enable_mcp`, `disable_mcp`, `enable_loops`, and `disable_loops` still change the list on the next call. MCP `input_schema` object keys are sorted when the server connects; array order is left as sent. The tool-schema digest no longer sorts keys, so a byte change on the wire is recorded. Across 12 idle calls with no compaction, the tools and system text were byte-identical, including across the tenth idle turn. Pre-push local CI passed. The dollar impact is still not measured. The finding text above is the review, not the current code.

### P5. Provider-specific gaps (Read). Merged.

- Bedrock Converse sends stable and volatile system text as one block before the cache point, so any volatile change or runtime note misses the system checkpoint.
- OpenAI gets no `prompt_cache_key`. `prompt_cache_retention=24h` is sent only when retention is `long`.
- Gemini uses implicit caching only.

Direction: split the Bedrock system into two blocks with the cache point after the stable one; send the thread id as `prompt_cache_key` on OpenAI.

Merged in [pull request 390](https://github.com/human-plus-machine/monkeybot/pull/390). The cache key is the session id, which is the thread id except on a branch. Gemini stays on implicit caching, and the scorecard reports the cache-read share. The status note under the summary has the rest. The finding text above is the review, not the current code.

### P6. Open question on Anthropic cache keys (Inferred)

Spike 2 saw 92–98% of the wire prefix kept in steady state once cache markers are included, against 100% of the content prefix. Whether Anthropic's cache key is affected by moving `cache_control` markers, or by a marked message switching between string and block form, is not known. One live call reading `cache_read_input_tokens` would settle it. The lookback limit is a risk only when one assistant message has more than about 20 blocks.

## 5. Performance

### F1. History is loaded and transformed twice per inner turn (Ran). Merged.

See the table in section 2. A quiet tool turn did two full loads, two transforms, two conversions, and two baseline composes. A turn in the 70–85% band did six loads. There is one more load per user message in `freeze_attachments_in_history`.

The request now keeps the raw rows from the first load (`RequestHistory` in `core/runtime/request_history.py`, wrapped once at the start of `_run_inner_core`). Later loads of that thread compare `last_row` (row count and the newest row's parsed content) and serve the snapshot when it matches. A mismatch, a failed or cancelled write, or `delete_rows` drops the snapshot. A store with no `last_row` is passed through, so the snapshot is never served unverified. The single-writer assumption is false: a voice call can append during the request, and a trusted cache would let compaction delete those rows. The post-tool token budget no longer reloads history when the prompt ledger can place the size.

Measured again on SQLite with the wrapper in place. A three-tool request (4 model calls, 6 seeded rows) did 1 full load and 7 probes. A turn in the 70–85% band that compacted did 1 full load and 8 probes. Before, each of those was a full load: 2 per quiet inner turn, 6 in the band, plus the end-of-request freeze. Postgres and Firestore were not run. The probe cannot see an outside rewrite that keeps the row count and the newest row. Covered by `tests/unit/core/runtime/test_request_history.py` and `T1-STORE-16`.

### F2. Token counting is the largest overhead (Ran, Read). Merged.

Each inner turn counted the prompt twice: once before the call and once after the tool batch (`tool_dispatch.py`).

- Claude and Gemini: each count is a network round trip that uploads the whole prompt. Latency was not measured.
- OpenAI and compatible: tiktoken runs on the event loop.

Measured on the OpenAI path with synthetic history, before the fix:

| History rows | One count | Inner-turn overhead | Overhead with a trivial counter |
|---|---|---|---|
| 50 | 8 ms | 17.8 ms | 1.9 ms |
| 200 | 32 ms | 70 ms | 4.6 ms |
| 1,000 | 168 ms | 359 ms | 18.6 ms |
| 3,000 | 507 ms | 1,096 ms | 61 ms |

Counting accounted for nearly all of the overhead.

The loop now keeps the provider-reported size and estimates the change. See the status note under the summary.

### F3. Synchronous work runs on the event loop (Ran). Merged.

tiktoken, JSON decoding of history rows, transform, compose, `palace.status()`, `_discover_skills`, and `_load_agent_md` all ran on the loop thread. From fact 5, each delays every session in the process.

`palace.status()` now runs in `asyncio.to_thread`. The file-tool half of the same problem is F4, which is merged. The rest landed in [pull request 392](https://github.com/human-plus-machine/monkeybot/pull/392). See the status note under the summary. Transform, compose, and the Claude and Bedrock conversions stay on the loop: each is under 5 ms at 850k tokens. The request-size pass and the Gemini conversion were not on this list. They were the largest pure-Python costs at that size, and the same pull request yields inside them.

### F4. Read tools are marked parallel but run one at a time (Read). Merged.

`read_file`, `grep`, `glob`, and `list_dir` are parallel-safe with a cap of 10, but each was called synchronously inside the async `execute` (`core_tool_executor.py` l.1337–1350 at the time of the review). `grep` used a blocking `subprocess.run` (`workspace_service.py` l.1702) with a timeout reported as 120 s. Parallel reads did not overlap, and a slow grep stalled every session.

`read_file` reads the whole file and then slices it (default 2,000 lines, maximum 5,000).

Direction for F3 and F4: run these in a worker thread, and run grep as an asyncio subprocess.

Merged in [pull request 368](https://github.com/human-plus-machine/monkeybot/pull/368). `read_file`, `grep`, `glob`, `list_dir`, and `load_file` run on the file-tool pool in `core/tools/fs_worker.py`, and parallel reads overlap. Grep still uses a thread, not an asyncio subprocess: the JSON parse and the per-file `stat` have to leave the loop too, and the Python walker needs a thread either way. Ripgrep is started with `Popen` so a cancel kills the child. The 120 s timeout and the `incomplete_scan` result are unchanged. A killed ripgrep does not fall back to the Python walker. The same pool runs the write tools, their before-image, the change record, and the spill write. The gateway file-viewer routes use it too.

### F5. Firestore multiplies the cost of F1 (Read). Merged.

Each load is one document read per row, so an inner turn costs at least twice the row count in reads. A compaction reset is about one read, one delete, and one append per row, and each append is a query, a transactional get, and two writes. Postgres does one SELECT per load and one transaction per reset.

F1 removes the repeated full loads. A later load is a `last_row` probe: one query on SQLite and Postgres, and a count plus one document read on Firestore, instead of one document read per row. The compaction reset is unchanged. Firestore probe latency was not measured.

**Merged.** F5 landed on `develop` in [pull request 388](https://github.com/human-plus-machine/monkeybot/pull/388) (`2ea4de69`). This note is not part of that pull request. The compaction-reset sentence above is the review. A compaction reads the thread once. When the archive copy, the live rewrite, the summary, and a same-scope legacy summary delete fit in 400 writes, they commit as one batch. Above that, the archive still commits before any live delete. A row written after the read is not in the snapshot, so it is not deleted. `last_row` runs its newest-row query and its count together. An append with no message id is one batch. An append with a message id starts the legacy message-id query before the transaction. The outbox append reads both documents in one batch get. `AsyncTransaction.get_all` raises `TypeError` on google-cloud-firestore 2.27, so that read uses `client.get_all` with the transaction. Appends that carry a message id now write the hashed summary id. `list_threads` keeps one entry when a raw-id summary is still present, and a rewrite or purge deletes a same-scope raw-id doc. On the emulator, a compaction went from 2 queries and 3 commits to 1 query and 1 commit, a plain append from 2 commits to 1, and an outbox append from 2 document gets to 1. A load plus 7 probes is still 8 queries and 7 counts; the two queries inside each probe overlap. Production latency was not measured. The per-request full load of N reads is unchanged. `T1-STORE-05` still fails on the emulator: a null `wing` is stored instead of rejected.

### F6. Each subagent is a new Python process (Ran). Deferred.

Import takes about 169 ms warm (`_openai_compat` is the largest at 45 ms); the gateway app takes about 310 ms. Each child opens its own storage backend, its own `MCPClient`, inspectors, and memory. MCP connect cost per child was not measured. Caps are 10 inline and 4 background; defaults are 1,000 turns and a 3,600 s timeout; subagents cannot nest.

Direction: measure MCP connect cost first. If it is large, share connections or keep a warm worker.

Deferred on 11 Nov 2026. No code was changed and no design was chosen. Notes from reading the spawn path and timing imports in one sandboxed process, for whoever picks this up:

- The 169 ms covers only the worker module. A child also imports its provider SDK on first use: `google.genai` about 530 ms, `anthropic` about 240 ms, `openai` about 150 ms. The `mcp` SDK adds about 320 ms when a server connects, and `chromadb` about 540 ms when memory is on. Code load is roughly 0.4 to 1.6 s of CPU per child. These were timed one after another, so the later figures benefit from the earlier imports. Approximate.
- A child connects only `autoConnect: true` servers, and waits for them in `build_context` before its first model call. Other servers stay catalogued until `enable_mcp`. That connect time, storage open, and provider TLS and auth are still not measured.
- The gateway rewrites `os.environ` on a config reload, so a pre-started worker would need the full environment sent with the task (Read).
- One spare worker does not shorten a parallel batch, because the parent waits for the slowest child (Inferred).
- On the parent loop, `write_subagent_pid` runs `ps` synchronously on macOS for each spawn, and `_append_progress_line` does a synchronous file append for each child event (Read).
- The two candidate designs are a warm spare worker that keeps one process per child, and running the child loop inside the gateway. The second removes the reconnects as well, but loses the process kill and puts child work on the gateway event loop.

### F7. Smaller per-tool costs (Ran). Merged.

- Write tools took a synchronous before-image of up to 2 MiB on the event loop. Merged with F4: the before-image, the write, and the change record run on the file-tool pool.
- PRE_TOOL hooks are awaited with a 1.5 s cap. POST_TOOL hooks are fire-and-forget with a 2.0 s settlement. Timed 1,000 `read_file` calls, each including the settlement drain, against a no-op executor. With no hook manager the median was 0.003 ms (p99 0.004 ms). The always-on gateway handlers (evidence guard, todo reminder, git context, undo note, read-ledger compaction) were 0.054 ms median (p99 0.087 ms). Adding the progress tracker and the verification gate was 0.062 ms median (p99 0.093 ms). The caps are not the cost. A repeat while the machine was busy stayed under 5 ms at p99. No change.
- `run_command`, write, web, and browser tools, and every provider call, wait on a loopback secret-egress scan. The call is off the event loop, but the turn waits for it. The timeout is 5 s, and a failure is not cached, so a hung bridge is paid again on the next scan. With no bridge file a scan was 0.007 ms. A bridge that answered at once took 0.34 ms median (p99 0.9 ms) over 20 distinct argument scans. The outbound scan of those same arguments was a cache hit, 0.003 ms median: both paths serialize with `json.dumps(args, ensure_ascii=False)`.

The hung case accepts the connection and never replies. Twenty argument scans plus twenty outbound scans took 200 s, about 5.003 s each.

Merged in [pull request 389](https://github.com/human-plus-machine/monkeybot/pull/389) (`5f0d9c18`). This note is not part of that pull request. A timeout or a connection error opens a breaker for that bridge URL and token for 30 s. Later scans in the window are not sent and are not remembered as clean, so the text is scanned again after the window. An HTTP status, including 500 and 413, does not open it: the bridge answered. A malformed body does not open it either. A text too large to send is still withheld without a round trip. The remaining batches of the scan that tripped the breaker are not sent. A new token is a different bridge and is scanned at once. One warning when the breaker opens, and one info line when a later scan gets an answer. The same 40 hung scans then took 5.0 s: one timeout of 5.005 s, a median of 0.07 ms for the argument scans, and 6 ms altogether for the outbound scans. Covered by `tests/unit/core/context/test_secret_egress.py`. Numbers are in `/tmp/monkeybot-spikes/s7b/` (`before.json`, `after.json`).

### F8. Per-message setup is cheap (Ran)

`_discover_skills` took 0.22 ms and `repo_overview` 1.35 ms. Memory wake-up cost was not measured.

### F9. First use of an unadvertised MCP server costs two model steps (Read). Accepted by design.

The model must call `enable_mcp` and then make the call. See P4 for the cache cost of the same step.

Accepted on 11 Nov 2026. No code change. Progressive disclosure is intentional: catalogued servers are named in the harness prompt and in the `enable_mcp` schema, but their tool definitions stay out of the request until the model connects the server. The model cannot call a tool it has not seen, and `enable_mcp` applies new tools on the next inner step (`core_tool_executor.py`), so the extra step is inherent to lazy loading, not a bug.

- **Latency:** one model call per server per thread on first use, not per MCP tool call.
- **Cache:** `enable_mcp` still changes the tool list once per activation; that rewrite is the P4 cost. Idle hiding no longer rewrites the list (P4 merged).
- **Removing the step** would require advertising every MCP tool schema up front, which defeats the goal of keeping disabled servers out of context.

Optional follow-ups if usage data shows waste (not scheduled): one-line capability hints per server in the harness catalog list; `enable_mcp` accepting multiple names in one call to cut steps and cache rewrites when several servers are needed. Not measured: typical extra-step latency, wrong-server `enable_mcp` rate, or parallel `enable_mcp` batching by the model.

## 6. Reliability

### R1. The loop never retries a failed model call (Ran). Merged.

A 429 on inner call 30 ended the turn after 29 successful rounds. Retries exist only inside adapters and only before the first byte:

| Provider | Retries before first byte |
|---|---|
| Anthropic, OpenAI | SDK default, 2 (connect errors, 408, 409, 429, 5xx) |
| OpenAI-compatible | Own loop, 3 attempts for 429 and 5xx |
| Gemini | None on status errors |

There is no mid-stream retry, no fallback model or provider, and nothing resumes interrupted work. History stays valid, so the next user message continues from where the turn stopped.

Direction, from fact 6: retry at the loop level with backoff when the failure is transient and no tool from that call has been dispatched.

Merged in [pull request 370](https://github.com/human-plus-machine/monkeybot/pull/370). The status note under the summary has the behaviour, the limits, and the settings.

### R2. A failed preflight count ends the turn (Ran). Merged.

`_maybe_compact_and_shape` has no error handling around the count (`turn_loop.py` l.1075). A failure ends the turn before the model is called. `run()` catches it and yields `Error` and `TurnComplete`. The post-batch recount is tolerant.

Direction: fall back to the local estimate. F2's direction removes most of these calls.

Merged in [pull request 387](https://github.com/human-plus-machine/monkeybot/pull/387). A failed count uses a local estimate and the turn still calls the model. The status note under the summary has the rest.

### R3. No timeout on a stalled stream (Ran, Read). Merged.

The harness has no idle timeout; `race_preempt` has none. Anthropic and OpenAI rely on the SDK's 600 s read timeout. The Gemini client is built with no `HttpOptions` (`providers/gemini.py` l.696 and l.703), so it has no timeout.

Direction: add an idle timeout in the loop (no chunk for N seconds) and set a timeout on the Gemini client.

Merged in [pull request 370](https://github.com/human-plus-machine/monkeybot/pull/370), with the retry in R1. The idle default is 300 seconds. The Gemini client timeout is a 900 second cap on the whole request. The status note under the summary has the rest.

### R4. Truncated text answers are delivered as complete (Ran). Merged.

A text-only answer cut off at the output limit is delivered with no continuation and no signal (`_handle_empty_or_final_text`, l.1808, does not check `stream_truncated`). Truncated tool batches are handled: they are rejected with a re-issue message, as are batches where every call failed to parse.

Direction: continue automatically or tell the user the answer was cut off.

Merged in [pull request 396](https://github.com/human-plus-machine/monkeybot/pull/396). A truncated text answer is continued up to twice, then delivered with an `inlineMessage` that it was cut off. The status note under the summary has the rest. The finding text above is the review, not the current code.

### R5. Raw exception text reaches the client (Read). Merged.

The SSE `Error` text is the raw exception string. Anthropic and OpenAI status errors include the response body. Only the OpenAI-compatible wrappers sanitize.

Merged in [pull request 396](https://github.com/human-plus-machine/monkeybot/pull/396). The client frame names the failure kind and keeps the HTTP status and request id. The raw body stays in the log. The status note under the summary has the rest. The finding text above is the review, not the current code. The realtime websocket, the subagent worker, and the config-reload admin error still send the raw text.

### R6. The doom-loop detector misses alternation (Read). Merged.

It catches 3 consecutive identical calls and repeated same-tool, same-path failures. It does not catch A-B-A-B.

Merged in [pull request 386](https://github.com/human-plus-machine/monkeybot/pull/386). The same two or three calls, repeated three times, now trip recovery. The status note under the summary has the rest.

### R7. Background jobs are killed at turn end (Read)

The cap is 4. The model must poll `await_command`. Running jobs are killed at turn end after up to 3 nudges.

Merged in [pull request 391](https://github.com/human-plus-machine/monkeybot/pull/391). Local commands on the SSE gateway outlive the turn. The status note under the summary has the rest.

## 7. Hypotheses the spikes refuted

These were my assumptions going in. Each was wrong.

| What I assumed | What is true |
|---|---|
| The sliding protected window reshapes rows on every call in the 70–85% band. | Entering the moderate tier reshapes older rows once; the next tool batch then compacts. |
| The system text always differs on the next user message. | Only when volatile state changed. With no change it is byte-identical. |
| The 5,000-row cap is the only guard after a failed summary. | It does not fire. There is no guard at all (C4). |
| Two oversized newest rows make summarization impossible. | Summarization needs only a non-empty middle (4 or more visible rows). |
| User images are stored as base64. | User uploads are `AttachmentRef`. Only `load_file` results hold base64, until the end-of-turn freeze. |
| Thread-scoped memory wake-up is the default. | Shared mode is the default. Thread-scoped applies only when `MONKEYBOT_MEMORY_SHARE_THREADS` is off. |
| An abort after a provider failure writes cancel envelopes. | It writes `interrupted_tool_result_text`. |
| A preflight count failure escapes the loop. | `run()` catches it and yields `Error` and `TurnComplete`. |
| The coding protocol is added by a mode switch. | It is always in the harness text. |
| `reset` reassigns row ids and is not atomic. | It keeps `row_id` and is atomic on SQLite and Postgres. |

## 8. What is still unverified

| Open item | What would settle it |
|---|---|
| Latency of the `count_tokens` round trips on Claude and Gemini (F2). | Time 20 live calls at two prompt sizes. |
| Anthropic cache-key behaviour for marker position and string-versus-block content (P6). | One live two-call run reading `cache_read_input_tokens`. |
| Cache hit rate and cost on Anthropic, Bedrock, and Gemini (P1–P5). | Run the t4 suite on each and read the usage fields the run files already record. |
| Whether a model follows date and memory in a user-role tail as reliably as in the system text (P3). | A live T4 run. The harness side is covered: a unit test changes the todo list, the git note, a memory drawer, and the date between two user messages. |
| The summarizer failure spiral (C5). | Script a failing summarizer over several turns. A single failed summary is covered in [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393). |
| Memory wake-up cost at scale (F8). `mempalace` is installed in the repo venv, but Spike 3 did not exercise it. | Time `refresh_memory_index` at 100 and 1,000 drawers. |
| MCP connect cost per subagent (F6). | Time `MCPClient` connect against the configured servers. |
| Firestore probe latency (F5). | Merged in [pull request 388](https://github.com/human-plus-machine/monkeybot/pull/388). The probe's two queries now run together. Emulator call counts are in the F5 note. Production latency was not timed. |
| Whether the loop is the only history writer during a request (F1). | Traced. It is not. A voice call writes the thread without the turn lock (`begin_rewrite` in `gateway/realtime/manager.py`); a chat turn does not check for a live call. Rewind, edit, purge, and scheduler ticks take the lock. Steers and verdicts write through the loop. Subagents use their own thread. The F1 snapshot is probed because of the voice call. Not tested: a rewrite that keeps the row count and the newest row, and a turn lock released as stale. |
| Token figures (section 2, C11). | Counted with `cl100k_base`. Recount with each provider's counter. |
| The fixes in "Direction" lines. | F4 is merged in [pull request 368](https://github.com/human-plus-machine/monkeybot/pull/368). `palace.status()` from F3 and the F7 before-image went with it. The rest of F3 is merged in [pull request 392](https://github.com/human-plus-machine/monkeybot/pull/392). R1 and R3 are merged in [pull request 370](https://github.com/human-plus-machine/monkeybot/pull/370). F1 and F2 are merged in [pull request 371](https://github.com/human-plus-machine/monkeybot/pull/371). The store-parity case T1-STORE-16 is merged in [pull request 372](https://github.com/human-plus-machine/monkeybot/pull/372). C4 and C5 are merged in [pull request 369](https://github.com/human-plus-machine/monkeybot/pull/369). C1 and P2 are merged in [pull request 374](https://github.com/human-plus-machine/monkeybot/pull/374). C9 is merged in [pull request 375](https://github.com/human-plus-machine/monkeybot/pull/375). The counting half of C8 is merged in [pull request 376](https://github.com/human-plus-machine/monkeybot/pull/376). The reference half of C8 is merged in [pull request 397](https://github.com/human-plus-machine/monkeybot/pull/397). C12 is merged in [pull request 377](https://github.com/human-plus-machine/monkeybot/pull/377). C6 is merged in [pull request 378](https://github.com/human-plus-machine/monkeybot/pull/378). C2 is merged in [pull request 379](https://github.com/human-plus-machine/monkeybot/pull/379). C7 is merged in [pull request 380](https://github.com/human-plus-machine/monkeybot/pull/380). P1 is merged in [pull request 381](https://github.com/human-plus-machine/monkeybot/pull/381). C13 is merged in [pull request 382](https://github.com/human-plus-machine/monkeybot/pull/382). C11 is merged in [pull request 383](https://github.com/human-plus-machine/monkeybot/pull/383). P4 is merged in [pull request 384](https://github.com/human-plus-machine/monkeybot/pull/384). P3 is merged in [pull request 385](https://github.com/human-plus-machine/monkeybot/pull/385). R6 is merged in [pull request 386](https://github.com/human-plus-machine/monkeybot/pull/386). F5 is merged in [pull request 388](https://github.com/human-plus-machine/monkeybot/pull/388). F7 is merged in [pull request 389](https://github.com/human-plus-machine/monkeybot/pull/389). R2 is merged in [pull request 387](https://github.com/human-plus-machine/monkeybot/pull/387). R7 is merged in [pull request 391](https://github.com/human-plus-machine/monkeybot/pull/391). P5 is merged in [pull request 390](https://github.com/human-plus-machine/monkeybot/pull/390). R4 and R5 are merged in [pull request 396](https://github.com/human-plus-machine/monkeybot/pull/396). The other Direction lines are not built. They are derived from the facts, not proven. |

## 9. Test gaps found along the way

These come from searching `tests/` by name and keyword, not from reading every test, so a differently named test could exist.

- The call after a mid-request context update is covered in [pull request 374](https://github.com/human-plus-machine/monkeybot/pull/374): `test_context_tail_stays_on_every_later_call` runs four calls, and T2-CTX-20 checks calls 2, 3, and 4. `test_loop_picks_up_refreshed_memory_between_turns` now runs three calls and checks that the system text is identical on all three. That change is in [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393), now merged.
- The duplicate wake-up and PRE_TURN recall (C12) is covered in [pull request 377](https://github.com/human-plus-machine/monkeybot/pull/377): T2-MEM-10 checks the fact appears once and the recall heading is absent, and `tests/unit/core/memory/test_recall_dedupe.py` checks both wake-up formats.
- The duplicate read and subagent text (C13) is covered in [pull request 382](https://github.com/human-plus-machine/monkeybot/pull/382): T2-FILE-38 checks the file text appears once and the stub once, and `tests/unit/core/runtime/test_read_dedupe.py` checks the cases that stay full.
- The capped todo list (C11) is covered in [pull request 383](https://github.com/human-plus-machine/monkeybot/pull/383): T2-CTX-22 checks that fifteen 500-character items are clipped on the next request and that starting one shows it in full. `tests/unit/core/runtime/test_prompt_sections.py` checks the per-section sizes.
- Alternating tool calls (R6) are covered in [pull request 386](https://github.com/human-plus-machine/monkeybot/pull/386): T2-LOOP-49 checks six `read_file` calls on two paths, and `tests/unit/core/runtime/test_doom_loop.py` checks cycles of two and three calls.
- A failed preflight count (R2) is covered in [pull request 387](https://github.com/human-plus-machine/monkeybot/pull/387): `tests/unit/core/runtime/test_preflight_count_failure.py` checks that the model is still called, and `test_prompt_ledger.py` checks the estimate. No T2 case was added.
- Background commands that outlive the turn (R7) are covered in [pull request 391](https://github.com/human-plus-machine/monkeybot/pull/391), now merged: `tests/unit/core/tools/test_session_jobs.py` checks that a job survives `aclose` and that a finish wakes once, and `tests/unit/gateway/sse/test_job_wake_drain.py` checks a job-only harness follow-up. T2-SHELL-14 and T2-LOOP-16 stay the kill-at-turn-end path, which the loop driver still uses.
- A request over the context window (C4) is covered in [pull request 369](https://github.com/human-plus-machine/monkeybot/pull/369): `tests/unit/core/runtime/test_context_overflow_loop.py` covers recovery, nothing left to compact, a second rejection, and a tiny window. [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393), now merged, adds the case where the summary then fails: `test_size_rejection_with_a_failing_summary_uses_the_fixed_error`, `test_size_rejection_when_the_summary_is_also_too_long`, T2-CTX-24 (`overflow_recovery`), and T2-CTX-25 (`overflow_summary_fails`). The user sees the fixed overflow error and history is not rewritten.
- A failed background summary falling back to an inline one is tested (`test_failed_background_job_falls_back_to_inline_summary`). An inline summary that fails (C4, C5) is covered in [pull request 393](https://github.com/human-plus-machine/monkeybot/pull/393), now merged: `test_a_failing_inline_summary_still_answers_and_keeps_history` checks the reply, no `Error`, `ContextSummarized` with `turns_summarized=0`, and the existing rows left in place. `test_summary_fold.py` already covered a failing summarizer that does not grow the request, halving, and three halvings that then propagate. T2-CTX-05 covers a summarizer that raises above 5,000 rows.
- `reset` tests assert row order and row ids on all backends. `message_id`, `turn_id`, and `created_at` are covered by `T1-STORE-17` in [pull request 378](https://github.com/human-plus-machine/monkeybot/pull/378) (C6).
- T4-CTX-01 and T4-CTX-02 assert `min_summarizations: 1` and are in the nightly suite. A live `--suite context` run on 11 Nov 2026 is `regression/t4/runs/a925e90f-1b9f-4ee5-90e4-a642b1c65e2b.json` (gitignored, commit `aa5695a0`, `ollama-cloud/glm-5.3-flash`). T4-CTX-01 passed with `summarizations_count` 77. T4-CTX-02 reported `Doom loop detected: tool 'read_file' called identically 3 times` and `summarizations_count` 0. The scenario file did not need a change: that error is a frame in a turn that still ends with `TurnComplete`, and the live runner stopped on the frame, so the scenario was never graded. Merged in [pull request 394](https://github.com/human-plus-machine/monkeybot/pull/394) (`e8d42adf`). The runner keeps reading until `TurnComplete`. An error with no `TurnComplete` is still the failure. The suite was not re-run. The original failure is in `test-runs/20261111T152006Z/20261111T152006Z-aa5695a-12523.jsonl`.

## 10. Spike index

All spikes were run by Grok 4.7 subagents, read-only on the repo. Scratch scripts are under `/tmp/monkeybot-spikes/` and will not survive a reboot or a temp cleanup.

| Spike | Question | Scripts | Findings |
|---|---|---|---|
| 1. Epoch update visibility | Does a mid-request update stay visible? | `s1/spike_epoch.py` | C1, P1, P3 |
| 2. Cache prefix stability | How much of the request prefix survives each kind of change? | `s2/spike_cache.py`, `s2/results.json` | P1–P6, C2 |
| 3. Per-turn overhead | What does one inner turn cost outside the model call? | `s3/measure.py`, `s3/followup.py` | F1–F3, F5, F8, C2, C3 |
| 4. Fixed prompt cost | How many tokens are spent before any conversation? | `s4/measure_fixed_tax.py`, `s4/measure_out.txt` | C11, C12 |
| 5. Compaction and overflow | Does compaction keep history valid, and what happens over the window? | `s5/h1_reset.py`, `h2_overflow.py`, `h2c_unsplittable.py`, `h3_h4_h5.py`, `h6_invariants.py`, `h6b_clean.py`, `h_repeat.py`, `h8_pending.py`, `h8_probe.py` | C4–C10 |
| 6. Provider failures | What happens on 429, stalls, truncation, and count failures? | `s6/spike_provider_failures.py` | R1–R5 |
| 7. Tools and subagents | What do tool dispatch and subagent start-up cost? | `s7/importtime-*.txt` | F4, F6, F7, F9, C13, R6, R7 |
