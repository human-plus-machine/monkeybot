# 6.U Gateway HTTP/SSE and the monkeyapp contract (`GW`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`).

Existing: `tests/gateway/**` uses `create_app(FakeLoopPort)` and `httpx.ASGITransport`. `FakeLoopPort` publishes via `agent_event_to_wire_dict`, which is **not** the production serializer (adds `chat_request_id`, dumps `usage` as a dataclass). `test_events.py` round-trips event JSON; nothing diffs a framed stream. Nothing checks monkeyapp's fake gateway against monkeybot.

Drift found between monkeyapp's fake gateway (`monkeyapp/tests/harness/fake-gateway.ts`) and production: `TurnComplete` without `usage`; `ContextSummarized.estimated_tokens` (not a production field); tool frames missing `label` and sometimes `call_id`; many frames missing `request_id`. The fake 404s on `/steer`, `/ask-user`, elicitations, frontend tool results, `DELETE /sessions`, `/usage`, workspace routes, `/api/chat-title`, `/scheduler/*`, `/goals/*`.

**Contract plan.**

1. `tests/contracts/test_sse_schema.py --update-golden` generates `tests/contracts/goldens/sse/schema.json` from `event_to_json` over every `AgentEvent` variant (Appendix B), plus framing rules.
2. monkeybot T1 validates every production event against it; T2 gateway invariant 6 validates every emitted frame.
3. monkeyapp vendors `schema.json` (copied by a script with the monkeybot commit in a header). Two monkeyapp unit tests: every frame the fake gateway emits validates; every `case` in `dispatchGatewayEvent` names a schema type.
4. Optional later: monkeyapp's fake gateway replays the `regression/t3/sse/*.sse` goldens instead of hand-built events.
5. Tests stop using `agent_event_to_wire_dict`; it is deleted or marked test-only and fixed to match.

| ID | Case | Asserts |
|---|---|---|
| T1-GW-01 | `event_to_json_schema` | Every kind validates; no `chat_request_id`; `TurnComplete.usage` has 8 keys. |
| T1-GW-02 | `lossy_fields_pinned` | `ToolCallStarted` omits `inspector_decision`/`resource`/`resolved_path`; `ToolCallResult` omits `error_kind`/`duration_ms`; `FileBlock` never has `data`; `SystemContextUpdated` omits `text`. |
| T1-GW-03 | `framing` | `ActiveRequests` and pings have no `id:`; garbage `Last-Event-ID` → full replay. |
| T1-GW-04 | `verdict_wire_snake_case` | SSE `VerifierVerdict` is snake_case (not `to_wire()` camelCase). |
| T1-GW-05 | `route_table_snapshot` | `app.routes` method+path set equals a committed list (Appendix D); a new/removed route is a reviewed diff. |
| T1-GW-06 | `error_envelope` | Every `APIError` code in routes has the `{error:{code,message,request_id}}` shape. |
| T2-GW-01 | `scripted_turn_golden_sse` | Gateway driver, scripted provider: frames equal `t3/sse/basic_turn.sse`. |
| T2-GW-02 | `tool_turn_golden_sse` | Tool call + result: equals `t3/sse/tool_turn.sse`. |
| T2-GW-03 | `setup_failure_releases_lock` | Broken MCP/provider: `Error` then empty `TurnComplete`; user prompt persisted; next `/reply` not 409. |
| T2-GW-04 | `reply_busy_409` | Second `/reply` during a turn: 409 `SESSION_BUSY`. |
| T2-GW-05 | `reconnect_last_event_id` | Reconnect with `Last-Event-ID: 2`: frames 1–2 skipped, rest replayed, fresh `ActiveRequests`. |
| T2-GW-06 | `replay_overflow_gap` | Past `SSE_REPLAY_MAX`: reconnect does not invent ids; gap documented (future `ReplayGap` event). |
| T2-GW-07 | `nested_lane_flood` | > 256 nested subagent frames: parent `TurnComplete` still replayable. |
| T2-GW-08 | `session_isolation` | Session B never sees A's `request_id`. |
| T2-GW-09 | `cancel_during_confirmation` | Cancel: terminal event; later confirm POST → 409 `STALE_PENDING_RESPONSE`. |
| T2-GW-10 | `steer_flow` | `/steer`: `QueuedInputAccepted(queue: steer)` then `UserSteered` with in-flight `request_id`; idle → 409 `SESSION_IDLE`; full → 429. |
| T2-GW-11 | `queue_flow` | `/queue` idle starts now (`position 0`); busy enqueues; promote sets `queued_request_id`; `DELETE` drops. |
| T2-GW-12 | `ask_user_route` | `AskUserRequest` → `POST /ask-user/{id}` → turn resumes; empty answer → 400 `EMPTY_ANSWER`. |
| T2-GW-13 | `elicitation_and_frontend_tool_routes` | `ActionRequiredEvent` / `FrontendToolRequest` round trips. |
| T2-GW-14 | `history_rewrite_order` | `HistoryRewritten` seq < first delta of the replay turn; voice and rewrite exclude each other. |
| T2-GW-15 | `branches_routes` | edit / regenerate / restore / switch / truncate / fork behaviors and 409s. |
| T2-GW-16 | `delete_session_quiesce` | `DELETE /sessions/{id}` during a turn: quiesces then cancels; idempotent. |
| T2-GW-17 | `usage_routes` | `/sessions/{id}/usage` and `/usage?bucket=` match recorded rows; hour bucket needs `since` ≤ 7 days. |
| T2-GW-18 | `workspace_and_history_api_toggles` | `MONKEYBOT_WORKSPACE_API` / `MONKEYBOT_CHAT_HISTORY_API` off → 404; workspace reads path-confined. |
| T2-GW-19 | `chat_title_route` | Title ≤ 60 chars; model failure → `{title: null}`, never 500. |
| T2-GW-20 | `health` | `/health` shape and memory detail. |
| T2-GW-21 | `slow_subscriber` | Non-reading subscriber: producer still finishes (documents unbounded queue growth). |
| T3-GW-01 | `trajectory_cassette_to_golden_sse` | A T3a cassette through the gateway driver produces the committed golden stream. |
| T3-GW-02 | (monkeyapp) `fake_gateway_validates` | Runs in monkeyapp CI against vendored schema. Fails today on `TurnComplete` without `usage` and `ContextSummarized.estimated_tokens`. |
| T3-GW-03 | (monkeyapp) `dispatch_cases_subset` | Every `dispatchGatewayEvent` case is a schema type. |
| T4-GW-01 | `one_turn_complete_per_request` | Every live scenario (global live invariant). |
| T4-GW-02 | `live_resume` | Drop the client mid-delta, reconnect with `Last-Event-ID`; concatenated deltas equal the final text. |
| T5-GW-01 | `cancel_storm` (N = 20) | Each trial: one terminal event, lock released. |
| T5-GW-02 | `reconnect_storm` (N = 20) | No duplicated `TurnComplete`, no lost `call_id` pairing. |
