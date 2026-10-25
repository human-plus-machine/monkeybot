# 6.V Realtime voice (`RT`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`.

Existing: T1 `tests/gateway/realtime/*`, `tests/core/test_realtime_loop.py`, `test_utterance_buffer.py`, `test_mocks_realtime_provider.py`, `tests/providers/test_gemini_live.py`. The realtime loop deliberately does not share doom-loop, empty-completion, compaction, epochs, final-answer gate, or steer drain with the text loop.

| ID | Case | Asserts |
|---|---|---|
| T1-RT-01 | `divergence_pinned` | Realtime loop does not import / call the text-loop guards; shares truncated-batch rejection, confirm wait, registry refresh. A refactor that changes this is a reviewed diff. |
| T2-RT-01 | `ws_text_turn` | WS `connect` → `text` → `text_delta`s → `turn_boundary`; `usage` frame. |
| T2-RT-02 | `ws_tool_turn` | `tool_call` → `tool_result` → continue. |
| T2-RT-03 | `ws_tool_confirmation` | `tool_confirmation` frame → `tool_confirmation_response`. |
| T2-RT-04 | `ws_interrupt` | `interrupt` → `interrupted`; partial assistant committed. |
| T2-RT-05 | `ws_slot_limit` | Over the slot limit: upgrade rejected before accept. |
| T2-RT-06 | `ws_guardrail_budget` | Budget exceeded: `session_ended` with reason. |
| T2-RT-07 | `ws_blocks_history_rewrite` | Live voice session: `DELETE /api/chat-history/{id}` → 409. |
| T4-RT-01 | `gemini_live_smoke` (deferred) | Requires GCP credentials; not runnable with the Ollama-only live setup (5.11). Listed under Deferred coverage in `docs/testing.md`. |
