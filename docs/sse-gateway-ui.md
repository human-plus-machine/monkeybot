# SSE gateway: HTTP API and custom UI integration

This document describes the **MonkeyBot v2 FastAPI gateway** endpoints used for chat, streaming, and playground helpers. It mirrors how the **playground chat UI** (`playground/chat-ui`) talks to the gateway so you can wire your own frontend.

**Reference implementation**

- HTTP helpers and SSE parsing: `playground/chat-ui/src/gatewayClient.ts`
- Session lifecycle and event handling: `playground/chat-ui/src/App.tsx`
- Route definitions: `src/monkeybot/gateway/sse/routes.py`
- Request/response models: `src/monkeybot/gateway/sse/models.py`
- Event payloads (`type` field on the wire): `src/monkeybot/core/runtime/events.py`

**Base URL**

- Default local playground port is **8787** (see `playground/agent/monkeybot_config/monkeybot.yaml`).
- All paths below are relative to the gateway origin, e.g. `http://127.0.0.1:8787`.

---

## CORS and dev proxy

**Cross-origin browser access**

- The production app sets CORS from `MONKEYBOT_CORS_ALLOW_ORIGINS` (comma-separated origins). If unset, the default allows `http://localhost:5173` (Vite’s default).
- Set this when your UI is on another host/port and calls the gateway **directly** without a reverse proxy.

**Playground dev (same origin)**

- `playground/chat-ui/vite.config.ts` proxies `/__mb_gateway` → your gateway (`VITE_GATEWAY_TARGET`, default `http://127.0.0.1:8787`) and strips the `/__mb_gateway` prefix.
- In dev, `gatewayClient.ts` uses `GATEWAY_BASE = '/__mb_gateway'` so the browser stays same-origin and avoids CORS.
- For production builds, set `VITE_GATEWAY_TARGET` to the full gateway URL (trailing slash is trimmed), or rely on the client default `http://127.0.0.1:8787`.

---

## JSON endpoints (non-SSE)

Errors use a common envelope:

```json
{ "error": { "code": "STRING", "message": "STRING", "request_id": "STRING" } }
```

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness; returns `{ "status": "ok", "version": "2.0.0" }`. |
| `POST` | `/sessions` | Create a session. Body (optional): `{ "session_id"?: string, "agent_md"?: string }`. **201** → `{ "session_id", "created_at" }` (`created_at` is Unix ms). **409** `SESSION_ALREADY_EXISTS` if you reuse an existing `session_id`. |
| `POST` | `/sessions/{session_id}/reply` | Start a user turn. Body: `{ "request_id": string, "message": string }` (message max 32000 chars). **200** → `{ "request_id" }`. **404** `SESSION_NOT_FOUND`. **409** `SESSION_BUSY` if a turn is already in progress. The agent runs asynchronously; progress arrives on the events stream. |
| `POST` | `/sessions/{session_id}/cancel` | Request cancellation for a turn. Body: `{ "request_id": string }`. **200** empty body on success. |
| `GET` | `/sessions/{session_id}/usage` | Aggregated usage for the session. Optional query: `since`. |
| `POST` | `/sessions/{session_id}/tool-confirmations/{tool_call_id}` | Approve/deny a tool. Body: `{ "approved": boolean, "reason"?: string }`. **202** `{ "ok": true }`. |
| `POST` | `/sessions/{session_id}/elicitations/{elicitation_id}` | Answer an elicitation. Body: `{ "user_data": any }`. **202** `{ "ok": true }`. |
| `POST` | `/sessions/{session_id}/frontend-tool-results/{tool_call_id}` | Return a frontend tool result. Body: `{ "result": ContentBlock[], "is_error": boolean }` (blocks are JSON objects matching `ContentBlock` schema). **202** `{ "ok": true }`. |
| `GET` | `/api/playground/workspace/tree` | Optional directory listing under the gateway workspace. Query `path` (repo-relative). Disabled when `MONKEYBOT_PLAYGROUND_WORKSPACE_API` is `0` / `false` / `no` / `off`. |
| `GET` | `/api/playground/workspace/file` | Optional file slice read. Query `path` (required), `offset` (1-based line, default 1), `limit` (default 200). Same env gate as tree. |

---

## SSE: `GET /sessions/{session_id}/events`

**Purpose**

- Long-lived **Server-Sent Events** stream of JSON payloads for the session: assistant deltas, tool lifecycle, errors, human-in-the-loop prompts, etc.

**Request**

- Method: `GET`
- Headers: `Accept: text/event-stream` (recommended; playground uses this with `fetch`).
- Optional: `Last-Event-ID: <integer>` — numeric sequence id of the last **buffered** data event you processed. The server replays buffered events with `id:` greater than this value, then continues live. Omit or use a fresh connection to receive the full replay buffer from the start (see `SessionBus.subscribe` in `session_bus.py`).

**Response**

- `Content-Type: text/event-stream`
- Framing:
  - **Numbered data events** (replayable): lines `id: <seq>` + `data: <json>` + blank line. Each `data` line is one JSON object.
  - **Heartbeats** (not replayed): comment frames like `: ping 1` — ignore for JSON parsing (playground’s `parseSseBlocks` skips lines starting with `:`).
  - **ActiveRequests** snapshot: a `data:` JSON object **without** an `id:` line, shape `{ "type": "ActiveRequests", "request_ids": string[] }`, sent after replay when you connect. Reflects in-flight work (typically zero or one id).

**Parsing**

- Split on SSE event boundaries (double newline). For each event block, read `data:` lines and `JSON.parse` the payload.
- The playground uses `fetch` + `ReadableStream` + `TextDecoder` in `consumeSseJson` so it can attach `AbortSignal` for clean teardown; `EventSource` also works if you do not need custom headers (use `Last-Event-ID` for resume).

**Environment**

- Replay buffer size: `SSE_REPLAY_MAX` (default `256` events).

---

## JSON event `type` values (SSE `data` payloads)

Each `data:` JSON object includes at least:

- `type` — same as the Python `AgentEvent` **kind** (e.g. `AssistantDelta`).
- `request_id` and `chat_request_id` — both set to the turn’s request id on the wire.

Common types you will handle in a chat UI:

| `type` | Role |
|--------|------|
| `Thinking` | Optional “thinking started” signal. |
| `AssistantDelta` | Incremental assistant text in `delta`. |
| `ToolCallStarted` / `ToolCallResult` | Tool name, args, result text / error. |
| `TurnComplete` | Turn finished; includes `usage` (tokens, cost, duration). |
| `Error` | Recoverable stream error string in `error`. |
| `ImageBlock` | Inline image (`mime_type`, base64 `data`). |
| `ThinkingBlockDelta` / `ThinkingBlockComplete` / `RedactedThinkingBlock` | Extended thinking blocks where the model exposes them. |
| `ToolConfirmationRequest` | User must approve/deny; POST to `tool-confirmations` with `tool_call_id`. |
| `ActionRequiredEvent` | e.g. `action_type: "elicitation"` with `id` and `payload`; POST to `elicitations/{id}`. |
| `FrontendToolRequest` | UI-executed tool; POST result to `frontend-tool-results/{tool_call_id}`. |
| `SystemNotificationEvent` | Toasts / inline system messages (`notification_type`, `msg`). |
| `ContextSummarizing` / `ContextSummarized` | Context window maintenance (optional UI). |
| `SystemPromptSnapshot` | Debug: full system prompt for an inner iteration (`inner_turn`, `text`). |

The canonical definitions are the `@dataclass` types in `src/monkeybot/core/runtime/events.py`.

---

## Recommended client flow (same as playground)

1. **Connect** — `POST /sessions` with `{}` or your own `session_id`. Store `session_id`.
2. **Open SSE** — `GET /sessions/{session_id}/events` in parallel with the rest of the UI; keep the connection open for the lifetime of the session (playground: `useEffect` on `sessionId` + `AbortController` on unmount).
3. **Send a message** — Generate a client-side `request_id` (the playground uses a short random id). `POST /sessions/{session_id}/reply` with `{ request_id, message }`.
4. **Render** — Append user message locally; merge `AssistantDelta` into a streaming buffer; on `TurnComplete`, finalize the assistant message and clear “busy” state.
5. **Stop** — `POST /sessions/{session_id}/cancel` with the active `request_id` (playground “stop” button).
6. **Human-in-the-loop** — When you see `ToolConfirmationRequest`, `ActionRequiredEvent` (elicitation), or `FrontendToolRequest`, show UI and call the matching POST endpoint; the agent continues and new events flow on the same SSE connection.

**Concurrency rule**

- Only **one** active `/reply` turn per session at a time (`SESSION_BUSY` otherwise). Keep `request_id` stable for that turn across cancel, confirmations, and streamed events.

---

## Quick sanity checks

```bash
# Health
curl -sS http://127.0.0.1:8787/health

# Session + first line of SSE (will include pings and JSON frames)
curl -sS -N -H 'Accept: text/event-stream' \
  http://127.0.0.1:8787/sessions/$(curl -sS -X POST http://127.0.0.1:8787/sessions \
  -H 'Content-Type: application/json' -d '{}' | jq -r .session_id)/events | head -n 20
```

For a full scripted walkthrough of sessions and tools from the shell, see [Getting Started](getting-started.md).
