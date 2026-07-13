# SSE gateway: HTTP API and custom UI integration

This document describes the **monkeybot v2 FastAPI gateway** endpoints used for chat, streaming, and workspace/history helpers, so you can wire your own frontend.

**Reference implementation**

- Route definitions: `src/monkeybot/gateway/sse/routes.py`
- Request/response models: `src/monkeybot/gateway/sse/models.py`
- Event payloads (`type` field on the wire): `src/monkeybot/core/runtime/events.py`
- Terminal client (built on this same API): `cli/src/monkeybot_cli/commands/chat.py`

**Base URL**

- Default local gateway port is **8080** (see CLI scaffold `monkeybot.example.yaml`).
- All paths below are relative to the gateway origin, e.g. `http://127.0.0.1:8080`.

---

## CORS and dev proxy

**Cross-origin browser access**

- The production app sets CORS from `MONKEYBOT_CORS_ALLOW_ORIGINS` (comma-separated origins). If unset, the default allows `http://localhost:5173` (Vite's default).
- Set this when your UI is on another host/port and calls the gateway **directly** without a reverse proxy.

**Local dev (same origin)**

- If your UI dev server proxies API calls (e.g. a Vite `server.proxy` entry), point it at the gateway origin (default `http://127.0.0.1:8080`) and strip your chosen prefix before forwarding.
- Same-origin proxying avoids CORS entirely; set `MONKEYBOT_CORS_ALLOW_ORIGINS` only when calling the gateway cross-origin.

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
| `POST` | `/sessions/{session_id}/reply` | Start a user turn. Body: `{ "request_id": string, "message"?: string, "content"?: ContentBlock[] }` — send **either** `message` **or** `content`, not both (message max 32000 chars). **200** → `{ "request_id" }`. See [Multimodal reply](#multimodal-reply) and error codes below. |
| `POST` | `/sessions/{session_id}/attachments` | Upload a session attachment (multipart). Field `file` (required). **201** → `{ attachment_id, mime_type, size_bytes, filename, created_at }`. Disabled when `ATTACHMENTS_ENABLED=false`. See [Attachments upload](#attachments-upload). |
| `POST` | `/sessions/{session_id}/cancel` | Request cancellation for a turn. Body: `{ "request_id": string }`. **200** empty body on success. |
| `GET` | `/sessions/{session_id}/usage` | Session aggregates and **last-turn** context hints (see [Session usage endpoint](#session-usage-endpoint)). Optional query: `since` (Unix ms). |
| `POST` | `/sessions/{session_id}/tool-confirmations/{tool_call_id}` | Approve/deny a tool. Body: `{ "approved": boolean, "reason"?: string }`. **202** `{ "ok": true }`. |
| `POST` | `/sessions/{session_id}/elicitations/{elicitation_id}` | Answer an elicitation. Body: `{ "user_data": any }`. **202** `{ "ok": true }`. |
| `POST` | `/sessions/{session_id}/frontend-tool-results/{tool_call_id}` | Return a frontend tool result. Body: `{ "result": ContentBlock[], "is_error": boolean }` (blocks are JSON objects matching `ContentBlock` schema). **202** `{ "ok": true }`. |
| `GET` | `/api/workspace/tree` | Optional directory listing under the gateway workspace. Query `path` (repo-relative). Disabled when `MONKEYBOT_WORKSPACE_API` is `0` / `false` / `no` / `off`. |
| `GET` | `/api/workspace/file` | Optional file slice read. Query `path` (required), `offset` (1-based line, default 1), `limit` (default 200). Same env gate as tree. |
| `GET` | `/api/chat-history` | Optional recent-threads listing. Disabled when `MONKEYBOT_CHAT_HISTORY_API` is `0` / `false` / `no` / `off`. |
| `GET` | `/api/chat-history/{session_id}` | Optional persisted user/assistant text for one thread. Same env gate as the list endpoint. |

---

## SSE: `GET /sessions/{session_id}/events`

**Purpose**

- Long-lived **Server-Sent Events** stream of JSON payloads for the session: assistant deltas, tool lifecycle, errors, human-in-the-loop prompts, etc.

**Request**

- Method: `GET`
- Headers: `Accept: text/event-stream` (recommended when using `fetch`).
- Optional: `Last-Event-ID: <integer>` — numeric sequence id of the last **buffered** data event you processed. The server replays buffered events with `id:` greater than this value, then continues live. Omit or use a fresh connection to receive the full replay buffer from the start (see `SessionBus.subscribe` in `session_bus.py`).

**Response**

- `Content-Type: text/event-stream`
- Framing:
  - **Numbered data events** (replayable): lines `id: <seq>` + `data: <json>` + blank line. Each `data` line is one JSON object.
  - **Heartbeats** (not replayed): comment frames like `: ping 1` — ignore for JSON parsing (skip lines starting with `:`).
  - **ActiveRequests** snapshot: a `data:` JSON object **without** an `id:` line, shape `{ "type": "ActiveRequests", "request_ids": string[] }`, sent after replay when you connect. Reflects in-flight work (typically zero or one id).

**Parsing**

- Split on SSE event boundaries (double newline). For each event block, read `data:` lines and `JSON.parse` the payload.
- `fetch` + `ReadableStream` + `TextDecoder` lets you attach an `AbortSignal` for clean teardown; `EventSource` also works if you do not need custom headers (use `Last-Event-ID` for resume).

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
| `TurnComplete` | Turn finished; includes `usage` (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`, `duration_ms`, `estimated_prompt_tokens` — see [Session usage endpoint](#session-usage-endpoint)). |
| `Error` | Recoverable stream error string in `error`. |
| `ImageBlock` | Inline image (`mime_type`, base64 `data`). |
| `ThinkingBlockDelta` / `ThinkingBlockComplete` / `RedactedThinkingBlock` | Extended thinking blocks where the model exposes them. |
| `ToolConfirmationRequest` | User must approve/deny; POST to `tool-confirmations` with `tool_call_id`. |
| `ActionRequiredEvent` | e.g. `action_type: "elicitation"` with `id` and `payload`; POST to `elicitations/{id}`. |
| `FrontendToolRequest` | UI-executed tool; POST result to `frontend-tool-results/{tool_call_id}`. |
| `SystemNotificationEvent` | Toasts / inline system messages (`notification_type`, `msg`). |
| `AttachmentDescriptor` | Frozen attachment metadata after an upload turn (`attachment_id`, `mime_type`, `filename`, `description`). Emitted once per ref when the turn ends; history stores a text descriptor line. |
| `ContextSummarizing` / `ContextSummarized` | Context window maintenance (optional UI). |
| `SystemPromptSnapshot` | Debug: full system prompt for an inner iteration (`inner_turn`, `text`). |

The canonical definitions are the `@dataclass` types in `src/monkeybot/core/runtime/events.py`.

---

## Recommended client flow

1. **Connect** — `POST /sessions` with `{}` or your own `session_id`. Store `session_id`.
2. **Open SSE** — `GET /sessions/{session_id}/events` in parallel with the rest of the UI; keep the connection open for the lifetime of the session (e.g. on mount, with an `AbortController` on unmount).
3. **Send a message** — Generate a client-side `request_id`. `POST /sessions/{session_id}/reply` with `{ request_id, message }` for text-only, or `{ request_id, content }` for multimodal (see [Multimodal reply](#multimodal-reply)).
4. **Render** — Append user message locally (include attachment chips when using `content`); merge `AssistantDelta` into a streaming buffer; on `TurnComplete`, finalize the assistant message and clear "busy" state. Upgrade attachment chips when `AttachmentDescriptor` arrives.
5. **Stop** — `POST /sessions/{session_id}/cancel` with the active `request_id` (e.g. a "stop" button).
6. **Human-in-the-loop** — When you see `ToolConfirmationRequest`, `ActionRequiredEvent` (elicitation), or `FrontendToolRequest`, show UI and call the matching POST endpoint; the agent continues and new events flow on the same SSE connection.
7. **Usage / context meter** — After `TurnComplete`, call `GET /sessions/{session_id}/usage` to update totals and the **pre-flight prompt token count** vs **summarization threshold** (see [Session usage endpoint](#session-usage-endpoint)).

**Concurrency rule**

- Only **one** active `/reply` turn per session at a time (`SESSION_BUSY` otherwise). Keep `request_id` stable for that turn across cancel, confirmations, and streamed events.

---

<a id="attachments-upload"></a>

## Attachments upload

`POST /sessions/{session_id}/attachments`

**Request**

- `Content-Type: multipart/form-data`
- Field `file` (required binary)

**Response `201`**

```json
{
  "attachment_id": "att_…",
  "mime_type": "image/png",
  "size_bytes": 12345,
  "filename": "screenshot.png",
  "created_at": 1710000000000
}
```

**Limits (v1)**

- Images: JPEG, PNG, GIF, WebP (max 20 MiB)
- PDF: max 50 MiB
- Max 50 attachments per session; max 5 refs per `/reply`

**Errors**

| Status | Code | When |
|--------|------|------|
| 404 | `SESSION_NOT_FOUND` | Unknown session |
| 404 | `NOT_FOUND` | `ATTACHMENTS_ENABLED=false` |
| 413 | `PAYLOAD_TOO_LARGE` | Over size cap |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | MIME not allowed or sniff mismatch |

Files are stored under `{workspace}/.monkeybot/attachments/{session_id}/{attachment_id}` with a JSON sidecar. TTL default 48h (`ATTACHMENT_TTL_HOURS`).

---

<a id="multimodal-reply"></a>

## Multimodal reply

`POST /sessions/{session_id}/reply` accepts **either**:

- `{ "request_id", "message": "plain text" }` — same as before, or
- `{ "request_id", "content": [ …ContentBlock… ] }` — structured blocks.

**Allowed block types in `content` (v1):** `text`, `attachmentRef` only. Inline `image` / `file` with `data` is rejected (`INLINE_ATTACHMENT_NOT_ALLOWED`).

**`attachmentRef` example**

```json
{
  "request_id": "req-1",
  "content": [
    { "type": "text", "text": "What is in this screenshot?" },
    {
      "type": "attachmentRef",
      "attachmentId": "att_abc…",
      "mimeType": "image/png",
      "metadata": { "filename": "bug.png" }
    }
  ]
}
```

**Rules**

- Multimodal replies may be **text only**, **attachments only**, or both.
- Do not send both `message` and `content` → `400 AMBIGUOUS_REPLY_BODY`
- Each `attachmentId` must exist for the session → `404 ATTACHMENT_NOT_FOUND`
- At most 5 `attachmentRef` blocks per reply → `400 TOO_MANY_ATTACHMENTS`
- `content` with only empty `text` blocks and no refs → `400 EMPTY_REPLY_BODY`

**Lifecycle**

1. Upload via `POST /attachments`.
2. Reply with `attachmentRef` — provider sees pixels on that turn only.
3. Turn end — history rewrites refs to frozen `text` descriptors; gateway emits `AttachmentDescriptor` SSE per attachment.
4. Later turns — agent uses `read_attachment` tool to reload pixels when needed.

---

<a id="session-usage-endpoint"></a>

## Session usage endpoint

`GET /sessions/{session_id}/usage`

**Purpose**

- Return **cumulative** token/cost totals for the session (optionally since `since`), plus fields that help a UI show **how full the conversation is** relative to the configured context window and the **same bar** the agent loop uses before **sync history summarization** (provider pre-flight input tokens).

**Query**

- `since` (optional): non-negative integer string, Unix **milliseconds**. When set, aggregates and “last turn” fields consider only `turn_usage` rows with `created_at >= since`.

**Response** (`SessionUsageResponse` in `src/monkeybot/gateway/sse/models.py`)

| Field | Meaning |
|--------|---------|
| `session_id` | Session id. |
| `turns` | Count of completed usage rows in scope. |
| `input_tokens` / `output_tokens` / `cached_tokens` | Sums of provider-reported tokens in scope. |
| `cost_usd` | Sum of `cost_usd` in scope. |
| `period_start` / `period_end` | Min/max `created_at` (ms) in scope (`0` when empty). |
| `last_prompt_tokens` | **Provider** `input_tokens` for the **most recent** completed turn (post-call usage on that request). |
| `estimated_prompt_tokens` | Peak **pre-stream** prompt input tokens for the latest turn: `Provider.count_input_tokens(messages, tools, model=…)` (Vertex **countTokens**, Anthropic **count_tokens**, OpenAI **tiktoken** on the Chat Completions-shaped payload). Updated whenever the loop builds the outbound bundle—including after an in-turn summarization rebuild. Same check as `ContextSummarizing` / `ContextSummarized`. |
| `summarization_threshold_tokens` | `floor(context_window_tokens × 0.85)` — same ratio as `SUMMARY_TRIGGER_RATIO` in `src/monkeybot/core/runtime/loop.py`. When `estimated_prompt_tokens` approaches or exceeds this, the user turn is near the **sync summarization** threshold (subject to history length and viability rules in the loop). |
| `context_window_tokens` | From gateway env `MODEL_CONTEXT_WINDOW` (YAML → runtime env). Denominator for “% of window” style UIs. |

**Notes**

- **`estimated_prompt_tokens` vs `last_prompt_tokens`**: `estimated_prompt_tokens` is the **pre-call** prompt size used for summarization and context meters; `last_prompt_tokens` is **post-call** provider-reported prompt usage for that completed request (billing-style). They are usually close but can differ (cache, multimodal, or API accounting). If `estimated_prompt_tokens` is `0` (e.g. old `turn_usage` rows before this was recorded), a UI may fall back to `last_prompt_tokens` for a meter.
- **Threshold is not a guarantee**: summarization also requires enough messages to compress and only runs at the start of an inner loop iteration when the estimate is ≥ threshold; see `src/monkeybot/core/runtime/loop.py`.

---

## Quick sanity checks

```bash
# Health
curl -sS http://127.0.0.1:8080/health

# Session + first line of SSE (will include pings and JSON frames)
curl -sS -N -H 'Accept: text/event-stream' \
  http://127.0.0.1:8080/sessions/$(curl -sS -X POST http://127.0.0.1:8080/sessions \
  -H 'Content-Type: application/json' -d '{}' | jq -r .session_id)/events | head -n 20
# Usage JSON (replace SID; meaningful after at least one completed turn)
curl -sS "http://127.0.0.1:8080/sessions/SID/usage" | jq .
```

For a full scripted walkthrough of sessions and tools from the shell, see [Getting Started](getting-started.md).
