# MonkeyBot Realtime / Micro-Turn Streaming Design

**Status:** Draft — not started. No code in this repo implements anything described here.
**Purpose:** Single source of truth for adding a continuous, low-latency, full-duplex conversational mode ("micro-turns," e.g. ~200ms input/output slices) alongside the existing turn-based harness. Open this before starting any step so later steps don't get broken by earlier ones.

**Related docs:** [Features](features.md) · [SSE Gateway](sse-gateway-ui.md) · [Cloud deployment](cloud-deployment-design.md) · [AgentCore](deploy-aws-agentcore.md)

---

## Why this is a new subsystem, not an extension of `loop.run()`

MonkeyBot's harness is **turn-based with streamed output**, not continuous/full-duplex:

- `POST /sessions/{id}/reply` takes one complete user message; the session is locked (`SESSION_BUSY`) until `TurnComplete` (`gateway/sse/routes.py`, `core/persistence/session_turn_locks.py`).
- `loop.run()` commits the user message to history atomically at turn start (`core/runtime/loop.py`), buffers tool calls until the provider signals `Done`, then executes them — there is no concept of a "partial," in-flight, or revocable utterance.
- Persistence is message-granular (`conversation_history`: one row per `Message`) — there is no time-sliced or sub-turn state.
- There is no audio content block, no STT/TTS, no VAD/endpointing, and no duplex transport (SSE is server→client only).

None of this is a bug — it's the correct design for a tool-using chat/agent harness. But it means true micro-turn (~200ms slice), interruptible, full-duplex conversation requires a **new, parallel control loop and transport**, not a modification of `loop.run()`. This document scopes that new subsystem while explicitly preserving the existing harness untouched.

---

## Architecture Model

```
┌────────────────────────────────────────────────────────────────┐
│                    EXISTING (unchanged)                        │
│  FastAPI SSE gateway ── loop.run() ── HistoryStore/UsageStore  │
│  Turn-based, tool-using, one message in / stream of tokens out │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│                    NEW: REALTIME GATEWAY LAYER                 │
│  WebSocket endpoint: /sessions/{id}/realtime                   │
│  Frame protocol: audio/text chunks in, partial+final out       │
│  Duplex session state machine (listen/think/speak/interrupted) │
└────────────────────────────────────────────────────────────────┘
                        │ calls (utterance-final only)
┌────────────────────────────────────────────────────────────────┐
│                    NEW: REALTIME HARNESS LOOP                  │
│  run_realtime() — persistent session, not request/response     │
│  Endpointing/VAD-driven turn-boundary detection                │
│  Reuses: Provider protocol (new realtime variant), tool        │
│  executor, memory, MCP — once an utterance finalizes            │
└────────────────────────────────────────────────────────────────┘
                        │ depends on (via protocols)
┌───────────────────────────┐  ┌──────────────────────────┐
│  REALTIME PROVIDER        │  │  UTTERANCE BUFFER         │
│  Persistent vendor session│  │  In-memory, per-session   │
│  (Gemini Live, OpenAI     │  │  Commits to HistoryStore  │
│  Realtime, Bedrock Nova   │  │  only at utterance         │
│  Sonic, ...)               │  │  boundary or interrupt     │
└───────────────────────────┘  └──────────────────────────┘
```

**Layering rule, extending the existing core dependency rule:** the realtime gateway layer must not be imported by `monkeybot.core`; the realtime harness loop lives in `core/runtime/` alongside `loop.py` as a sibling, not a modification, and reuses `Provider`, `ToolExecutorPort`, `HistoryStore`, and `MemorySubsystem` protocols already defined in the harness.

---

## Configuration Strategy: One `monkeybot.yaml`, One Active Harness

The realtime and turn-based harnesses share almost all configuration: `model`, `paths`, `memory_hook`, `subagent`, `tools`, `web_search`, `sandbox`, etc. Forcing operators to maintain two separate files would create drift and confusion. Therefore, **the same `monkeybot.yaml` is used**, with a required top-level switch that selects the active harness for the deployment.

```yaml
# Required. Selects the conversational harness for this deployment.
#   turn_based  — existing HTTP POST /reply + SSE (default, backwards-compatible)
#   realtime    — WebSocket /realtime with full-duplex audio/text
harness:
  mode: turn_based

# Realtime-specific settings are ignored when mode is turn_based,
# but validated at startup so a misconfigured realtime deployment fails fast.
realtime:
  # WebSocket gateway settings
  websocket:
    enabled: true
    port: 8080                      # default: same as runtime.port
    # If true, upgrade is allowed; if false, route returns 404.
    # Equivalent env: MONKEYBOT_REALTIME_WS_ENABLED
    # Auth: reuses the same gateway auth middleware as /reply (see Operational Model).

  # Audio contract between client and gateway
  audio:
    # Format the gateway expects from the client. The gateway may transcode to the
    # provider's required format, but the client contract is fixed per deployment.
    input_format: pcm_s16le_24khz_mono
    output_format: pcm_s16le_24khz_mono
    # Max duration of a single utterance before the gateway forcibly finalizes it.
    max_utterance_sec: 60
    # Chunk size expected from client (ms). 200ms is the framing interval.
    chunk_ms: 200

  # Session lifecycle / cost guardrails
  session:
    # Max duration a realtime session may stay open. Hard cap; gateway closes.
    max_duration_sec: 1800
    # If no audio/text is received for this long, the session is closed.
    idle_timeout_sec: 120
    # If the model is speaking and the user is silent, keep the turn open up to this long.
    max_response_turn_sec: 300
    # Max concurrent realtime sessions per process. Additional upgrades are rejected with 503.
    max_concurrent_sessions: 100

  # Observability
  metrics:
    # Emit a realtime-session metric summary on close. Uses existing logger conventions.
    emit_summary_on_close: true
```

**Why not a separate `monkeybot_realtime.yaml`?** Because the two harnesses share the agent definition (`AGENT.md`), tool set, memory store, subagent config, and model provider family. A separate file would duplicate those sections and almost certainly drift out of sync. If realtime-specific settings ever become large enough to dominate the file (e.g., 80% of keys), that is the time to split — not at v1.

**Migration / default behavior:** Existing configs without `harness.mode` default to `turn_based`. Adding `realtime.*` settings to a `turn_based` config is allowed and validated, but they have no effect. This makes it safe to stage a realtime config before flipping the mode flag.

---

## Non-Negotiable Design Constraints

1. **`loop.run()` and the existing `/reply` + SSE path are not modified.** This is additive architecture. Existing turn-based deployments (Pattern A/B/C) must see zero behavior change.
2. **No new global singletons.** The realtime session state machine lives per-`SessionBus`-equivalent object, analogous to today's `SessionRegistry` (in-memory, single-process; multi-instance requires an external pub/sub, same caveat already noted in `session_bus.py`).
3. **Utterance-boundary commits only.** Partial/in-flight audio or text is never written to `HistoryStore`. History still only ever contains complete, finalized `Message` rows — this preserves every existing invariant in `docs/features.md` (`ToolRequest`/`ToolResponse` pairing, tool-call ordering, summarization, memory hooks) without touching them.
4. **Tool execution is post-utterance only; no mid-tool-execution interruption.** Tools run after an utterance is finalized (VAD/endpoint signal), same as today's "after `Done`" semantics — just triggered by an endpoint event instead of a provider stream end. Once a tool call is dispatched, the user cannot interrupt it — this is a permanent design decision, not a v1 limitation to revisit later.
5. **Realtime provider is a new, separate protocol — not a `stream()` shape.** `Provider.stream()` is request/response-per-call; realtime vendor APIs (Gemini Live, OpenAI Realtime, Bedrock Nova Sonic) are persistent duplex sessions. Do not force-fit them into the existing `Provider` protocol. Define a new `RealtimeProvider` protocol instead.
6. **Transport is WebSocket. Not WebRTC.** MonkeyBot's target deployments (Cloud Run/ECS server-to-server, Vertex AI Agent Engine `bidi_stream_query`, Bedrock AgentCore `/ws`) are all WebSocket-native. WebRTC only pays off for direct-from-browser mic capture over lossy networks, which is not a MonkeyBot use case. (This could be revisited if direct browser mic capture becomes a first-class requirement.)
7. **Gateway-only feature flag.** Realtime support ships as an optional gateway route + optional extras (`monkeybot[realtime]`), same pattern as `[postgres]`/`[gcs]`. Zero impact on default install size or behavior for existing users.

---

## What This Unlocks vs. What's Explicitly Out of Scope

**Unlocks:**
- Low-latency voice/text conversational agents with human-like interruption handling.
- A path to deploy on GCP Vertex AI Agent Engine (`bidi_stream_query`, `EXPERIMENTAL` server mode) and AWS Bedrock AgentCore Runtime (`/ws` endpoint) — both already support this pattern platform-side.

**Permanently out of scope:**
- Interrupting a tool call once dispatched (constraint 4) — not a v1 gap, a fixed design decision.

**Out of scope for v1:**
- True 200ms-granularity model reasoning — 200ms is a **transport framing interval**, not a claim that the LLM reasons every 200ms. Semantic turns (what the model actually responds to) are still utterance-level, bounded by VAD/endpointing, typically 500ms–2s of silence.
- Multi-instance/horizontally-scaled realtime session affinity (same caveat as today's in-process `SessionRegistry`; sticky routing or external state is a deployment concern, not this design's).

---

## Step 1: `RealtimeProvider` Protocol

**Goal:** Define the vendor-agnostic contract for persistent duplex model sessions, parallel to (not replacing) `core/llm/provider.py`.

### What changes

New module `core/llm/realtime_provider.py`:

```python
from dataclasses import dataclass
from typing import AsyncIterator, Protocol, Sequence

from monkeybot.core.tools.tool_def import ToolDef


@dataclass(frozen=True)
class AudioFormat:
    encoding: str          # "pcm_s16le" | "pcm_s16be" | "mulaw" | ...
    sample_rate_hz: int    # 16000 | 24000 | ...
    channels: int          # 1 | 2
    frame_ms: int          # 20 | 40 | 200


@dataclass(frozen=True)
class RealtimeSessionConfig:
    model: str
    system_prompt: str
    tools: Sequence[ToolDef]
    # The vendor may support only a subset of formats; the adapter must pick the
    # closest supported format and expose what it actually selected.
    preferred_input_format: AudioFormat
    preferred_output_format: AudioFormat
    # Optional: voice id, output modality, session truncation, etc.
    voice: str | None = None
    max_output_tokens: int | None = None


class RealtimeProvider(Protocol):
    async def connect(self, *, config: RealtimeSessionConfig) -> RealtimeSession: ...


class RealtimeSession(Protocol):
    # Actual negotiated formats (may differ from preferred_* if the vendor
    # required a fixed format).
    @property
    def input_format(self) -> AudioFormat: ...
    @property
    def output_format(self) -> AudioFormat: ...

    async def send_audio(self, chunk: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    # Injects a system/tool result into the live session without a user turn.
    async def send_context(self, text: str) -> None: ...
    # Tells the provider the current turn is canceled due to interruption.
    async def interrupt(self) -> None: ...
    # Events from the provider. Order is significant.
    def events(self) -> AsyncIterator[RealtimeEvent]: ...
    async def close(self, *, reason: str = "session_end") -> None: ...
```

- `RealtimeEvent` union lives in `core/llm/realtime_provider.py` alongside `RealtimeProvider` and `RealtimeSession`, tagged distinctly (e.g. `RealtimePartialTranscript`, `RealtimeAudioDelta`, `RealtimeTurnBoundary`, `RealtimeInterrupted`, `RealtimeError`). Gateway code will wrap these provider-level events into its own wire events before serialization.
- The protocol deliberately exposes **actual** negotiated `input_format`/`output_format` because vendors often require a fixed format (e.g., Gemini Live audio is 24kHz PCM). The gateway uses the negotiated format to drive its client audio contract or transcoding decisions.

### First implementation — decided: Gemini Live

Matches existing `google_vertexai` provider family and Vertex AI Agent Engine's native `bidi_stream_query` pattern. Gated behind `monkeybot[realtime-gemini]` extra (`websockets` dependency, no new heavy SDKs by default). OpenAI Realtime / Bedrock Nova Sonic remain future `RealtimeProvider` implementations behind their own extras, not required for v1.

The Gemini Live adapter is responsible for:
- Mapping Gemini Live server events (`serverContent`, `serverToolCall`, `interrupted`, `error`, etc.) to the `RealtimeEvent` union.
- Mapping `RealtimeSession` calls to Gemini Live client events (`clientContent`, ` realtimeInput`, `interrupt`).
- Injecting tool results as `clientContent` with the tool response attached.

### Tests

- Fake `RealtimeSession` double (mirrors `ScriptedFakeProvider` pattern in `core/testing/mocks_provider.py`) for deterministic unit tests without a live vendor session.
- A short, env-flag-gated integration test that opens a real Gemini Live session and verifies the adapter can send audio and receive a non-empty event stream. This test is off by default so CI does not become flaky or expensive.

---

## Step 2: Utterance Buffer & Turn-Boundary Detection

**Goal:** Bridge continuous input chunks to the existing message-granular `HistoryStore` without touching its schema or invariants.

### What changes

New module `core/runtime/utterance_buffer.py`:
- Accumulates incoming audio/text chunks per session in memory only.
- Detects turn boundaries via the realtime provider's own endpointing (Gemini Live, OpenAI Realtime, and Nova Sonic all emit end-of-turn / speech-stopped signals — reuse vendor VAD rather than building a custom one for v1).
- On boundary: hands the finalized utterance to a new `run_realtime_turn()` entry point that mirrors `loop.run()`'s post-utterance behavior (tool dispatch, history append, memory hooks) but is driven by a boundary event, not an HTTP POST.
- On interrupt (user starts speaking while agent is speaking): discards in-flight output, does **not** partially commit history, emits an `Interrupted` event. The user's new utterance that caused the interrupt is **kept** in the buffer and continues to accumulate; it is committed normally at the next boundary. This is the only sensible UX: the user is replacing the agent's response with a new request.
- `task` calls dispatch through the existing subagent path without blocking the duplex session; completion is delivered back on next session-idle (see "Non-blocking subagent dispatch" below).

### What does NOT change

- `HistoryStore`, `Message`, `ContentBlock` — untouched. Finalized utterances become ordinary `Message` rows exactly as today.
- `core/tools/core_tool_executor.py`, `core/subagents/subagent_proto.py`, `subagent_worker.py`, `RunStore` — all reused as-is once an utterance is finalized.

---

## Step 3: Realtime Gateway Transport

**Goal:** New WebSocket route, isolated from the existing SSE routes.

### What changes

- New route: `WS /sessions/{session_id}/realtime` in a new `gateway/realtime/` package (sibling to `gateway/sse/`, not inside it — keeps the existing SSE code path untouched and independently testable).
- Frame protocol: binary audio chunks + JSON control/text frames, similar to AgentCore's `/ws` and Gemini Live's client protocol.
- Session state machine per connection: `listening → thinking → speaking`, with `interrupted` as a transition from `speaking`/`thinking` back to `listening`.
- Reuses `SessionRegistry`-style per-process session tracking; explicitly does **not** reuse `session_turn_locks.py` (that lock models "one exclusive HTTP turn," which doesn't apply to a persistent duplex connection).
- Auth: reuses the same gateway auth middleware as the existing `/reply` SSE routes. The WebSocket upgrade request is authenticated before the connection is accepted; if auth fails, the upgrade is rejected with 403/401. No separate auth mechanism is introduced for realtime.

### What does NOT change

- `gateway/sse/routes.py`, `session_bus.py`, `app.py` — zero modifications. Existing `/reply` + SSE clients are unaffected.

---

## Operational Model

The realtime subsystem is a long-lived process with a client WebSocket on one side and a vendor realtime session on the other. The following rules must be specified in code, not left to default library behavior.

### Session lifecycle

1. **Setup.** Client opens `WS /sessions/{id}/realtime`. Gateway validates auth, creates or loads the `Session` object, and creates a `RealtimeSession` via the configured `RealtimeProvider`. If provider connection fails, the WebSocket is closed with a clear close code + error payload.
2. **Active.** The state machine runs. Audio/text flows in; provider events flow out. The gateway is responsible for keepalive: if neither side has sent data for a configurable interval, it sends a ping frame and expects a pong; if the client misses pongs, the gateway closes the connection.
3. **Idle.** If the client stops sending audio/text for `realtime.session.idle_timeout_sec`, the gateway sends a warning and then closes the connection. This is a cost guardrail, not a user-visible timeout.
4. **Hard cap.** If the session reaches `realtime.session.max_duration_sec`, the gateway closes the connection gracefully and sends a `SessionEnded` event with reason `max_duration_exceeded`.
5. **Teardown.** On close (client disconnect, idle timeout, max duration, error, or explicit `close` frame), the gateway:
   - closes the provider session (`await session.close()`)
   - discards any uncommitted utterance buffer
   - persists any queued subagent results via `RunStore` if not yet delivered
   - emits a final `SessionEnded` event to the client if still connected

### Reconnection behavior

- **v1: no reconnect/resume.** A WebSocket disconnect ends the realtime session. The client can create a new session via the normal session creation API and reconnect to a fresh `WS /sessions/{new_id}/realtime`. The new session starts with the existing `HistoryStore` history for that session ID, but the live provider session is new. This matches the existing session model and avoids distributed state complexity.
- **Future:** If reconnect becomes required, design a sticky load-balancer + external session-state store (Redis). This is explicitly out of scope for v1 (see Open Questions).

### Error handling

Every error in the realtime loop must be either (a) surfaced to the client as a typed `RealtimeError` event and the session closed, or (b) logged and swallowed only if it is a non-fatal client-side event (e.g., malformed client frame). There are no bare `except Exception: pass` blocks. The error taxonomy:

| Error | Client-visible | Action |
|---|---|---|
| Provider connection failure | Yes (close with reason) | Close WS, log error |
| Provider stream error | Yes (`RealtimeError` event) | Close provider session, close WS |
| Client protocol violation | Yes (close code 1008) | Close WS, log warning |
| Audio format mismatch | Yes (close with reason) | Close WS |
| Subagent dispatch failure | Yes (`RealtimeError` event) | Log, continue session (subagent failure is not fatal) |
| Gateway internal error | Yes (close code 1011) | Log, close WS |

### Cancellation

- On client disconnect, all in-flight tasks are canceled.
- On user interrupt, the current provider turn is canceled via `RealtimeSession.interrupt()`; the provider session itself remains open.
- On session close, the provider session is closed and all pending provider tasks are awaited with a timeout.

---

## State Machine & Interrupt Semantics

The session state machine is the single source of truth for what the gateway is doing. It lives in `gateway/realtime/session_state.py`.

### States

- `listening` — waiting for user input. The provider is in an open, idle state (no active turn).
- `thinking` — provider is processing a finalized user utterance; no audio output yet.
- `speaking` — provider is emitting output (audio or text). User can interrupt.
- `interrupted` — transient state after the user barge-in. Immediately transitions back to `listening` once the previous turn is fully canceled.
- `tool_running` — post-utterance tool calls are in flight. This is a sub-state of `thinking` (from the user perspective) but tracked separately because tools cannot be interrupted once dispatched.
- `closing` — teardown in progress. No new input accepted.

### State transitions

| From | Event | To | Notes |
|---|---|---|---|
| `listening` | user audio/text chunk arrives | `listening` | accumulate in utterance buffer |
| `listening` | provider turn boundary (utterance finalized) | `thinking` | hand to `run_realtime_turn()` |
| `thinking` | provider emits first output delta | `speaking` | can be text or audio |
| `speaking` | user audio/text chunk arrives | `interrupted` | cancel in-flight output |
| `thinking` | user audio/text chunk arrives | `interrupted` | cancel in-flight computation |
| `interrupted` | provider acknowledges interrupt | `listening` | keep user's new utterance in buffer |
| `speaking` / `thinking` | provider turn boundary (model finished naturally) | `listening` | commit to history, run memory hooks |
| `thinking` | tool calls dispatched | `tool_running` | tools run post-utterance only |
| `tool_running` | tool results ready | `thinking` | inject via `send_context()`; model responds |
| any non-closing | max duration / idle timeout | `closing` | send `SessionEnded`, close |
| any non-closing | client disconnect | `closing` | close provider, clean up |
| `tool_running` | user interrupt | `tool_running` | ignored; constraint 4: no mid-tool interruption |

### Interrupt semantics in detail

When the user interrupts while the agent is `speaking` or `thinking`:
1. Gateway sends `interrupt()` to the provider session.
2. Provider stops emitting output for the current turn. Any partial audio/text is discarded by the gateway; **nothing is written to `HistoryStore` for that turn.**
3. The user's new input (the audio/text that caused the interrupt) is **kept** in the utterance buffer and continues to accumulate.
4. Gateway emits an `Interrupted` event to the client.
5. Once the provider confirms the turn is canceled, the state returns to `listening`.
6. The next turn boundary event finalizes the user's new utterance and commits it as a normal `Message`.

This preserves the invariant that `HistoryStore` only contains complete, finalized utterances, and it gives the user the expected "I cut off the agent and asked something else" experience.

---

## Audio Format Contract

The client and gateway must agree on an audio format. The gateway does not negotiate per connection; the format is fixed by `realtime.audio.*_format` in `monkeybot.yaml`. The gateway may transcode between the client format and the provider format, but the client contract is stable per deployment.

```yaml
realtime:
  audio:
    input_format: pcm_s16le_24khz_mono
    output_format: pcm_s16le_24khz_mono
    chunk_ms: 200
    max_utterance_sec: 60
```

Supported v1 formats:
- `pcm_s16le_24khz_mono` — PCM 16-bit little-endian, 24kHz, mono. This matches Gemini Live.
- `pcm_s16le_16khz_mono` — for clients that prefer 16kHz; gateway transcodes if provider requires 24kHz.

Future formats (not v1): `mulaw_8khz_mono`, `opus_48khz_stereo`, etc.

**Frame layout:**
- Binary frames carry audio chunks. Each frame is exactly one chunk of `sample_rate * (chunk_ms / 1000) * channels * bytes_per_sample` bytes.
- JSON control frames carry text input, client events, and configuration. The first message from the client is a `connect` JSON frame containing the requested session ID; the gateway responds with a `connected` frame containing the negotiated audio formats.
- Text can be sent either as a JSON control frame or via a dedicated `text` JSON frame. Audio and text are not mixed in the same frame.

---

## Tool Result Injection

Subagents and tools run **after** the user utterance is finalized. Their results must be returned to the model, which is still in a live session. The path is:

1. User utterance finalizes; `run_realtime_turn()` is called.
2. If a tool/subagent is needed, it is dispatched through the existing tool executor and subagent worker (`core/subagents/subagent_proto.py` / `subagent_worker.py`).
3. The realtime session is **not** held open waiting for the tool. The model may continue the conversation with a filler/acknowledgment, or the harness may inject a short acknowledgment text via `send_text()` if the model does not.
4. When the tool/subagent completes, the result is queued for the session.
5. The harness injects the result into the live provider session via `RealtimeSession.send_context()` only when the session reaches **true idle** (`listening` state with no active user utterance in progress). The provider then generates a response to the tool result as a normal turn.
6. That tool-response turn is committed to `HistoryStore` exactly like a user/model turn, preserving the existing `ToolRequest`/`ToolResponse` pairing invariants.

**Why `send_context()` and not `send_text()`?** Because the tool result is not a user message; it is system/context information that the model should respond to. Some providers treat these differently (e.g., Gemini Live has a `clientContent` role distinction). The protocol exposes `send_context()` so the adapter can use the correct provider-level mechanism.

**Delivery guarantee:** If the realtime session closes before the result is delivered, the result is already persisted in `RunStore` / `HistoryStore` via the existing turn-based path. The client can retrieve it via the existing session history API. Nothing is lost.

---

## Cost & Concurrency Guardrails

Realtime sessions are expensive and long-lived. The following guardrails are mandatory and configured in `monkeybot.yaml`:

```yaml
realtime:
  session:
    max_duration_sec: 1800        # hard cap per session
    idle_timeout_sec: 120           # no input for 2 minutes -> close
    max_response_turn_sec: 300    # one model turn cannot exceed 5 minutes
    max_concurrent_sessions: 100    # per process; 503 on exceed
    # Optional: max audio seconds per session or per minute (future)
    # max_audio_seconds_per_session: 600
```

- **Max duration:** when reached, the gateway closes the provider session and the WebSocket with a `SessionEnded` event. The client must create a new session to continue.
- **Idle timeout:** protects against clients that leave a connection open without interacting.
- **Max response turn:** prevents a runaway model monologue from holding the session indefinitely.
- **Max concurrent sessions:** bounded by a semaphore in the gateway. If the limit is reached, new WebSocket upgrades are rejected with 503 and a `Retry-After` header.

---

## Observability & Metrics

The realtime subsystem reuses existing MonkeyBot logging conventions (structured JSON logs, `logger.info/error` with `{"event": ...}` fields). New metrics are emitted at the session and per-turn level.

### Session-level metrics (emitted on close)

- `realtime_session_duration_sec` — total session lifetime.
- `realtime_session_user_audio_sec` — total user audio sent.
- `realtime_session_model_audio_sec` — total model audio generated.
- `realtime_session_interrupt_count` — number of user interrupts.
- `realtime_session_close_reason` — `client_disconnect`, `idle_timeout`, `max_duration`, `error`, `explicit`.
- `realtime_session_turn_count` — finalized utterance count.

### Per-turn metrics

- `realtime_turn_latency_ms` — time from final boundary event to first output delta.
- `realtime_turn_model_duration_ms` — time from first output delta to turn boundary.
- `realtime_turn_tool_count` — number of tool calls in the turn.
- `realtime_turn_subagent_latency_ms` — subagent result wait time (if any).

### Critical events to log

- WebSocket connect / disconnect.
- Provider connect / disconnect / error.
- State transitions (especially `interrupted`).
- Tool/subagent dispatch and result delivery.
- Guardrail triggers (max duration, idle timeout, concurrency limit).

---

## Decisions

| Question | Decision |
|---|---|
| Which realtime vendor first — Gemini Live or OpenAI Realtime? | **Gemini Live.** Aligns with existing `google_vertexai` provider investment and Agent Engine's native `bidi_stream_query` support. |
| Does `task`/subagent dispatch make sense mid-conversation? | **Yes, non-blocking.** Main realtime loop dispatches `task` the same way `loop.run()` does today, but does not block the duplex session waiting on it — see "Non-blocking subagent dispatch" below. |
| When does a completed subagent result get delivered if the user is still talking? | **Only at true session-idle.** No max-wait timeout, no interrupting the user. The result waits in `RunStore` until the session state machine reaches `listening` with no active utterance in progress. |
| Same `monkeybot.yaml` or separate realtime config? | **Same file, with a required `harness.mode` switch.** Realtime and turn-based share almost all config; a separate file would duplicate and drift. |
| WebSocket auth mechanism? | **Reuse existing gateway auth middleware.** The WebSocket upgrade is authenticated before the connection is accepted. |
| Reconnect/resume support? | **Not in v1.** A disconnect ends the realtime session; the client starts a new session. This avoids distributed state complexity. |
| Client audio format negotiation? | **Fixed per deployment in `monkeybot.yaml`.** The gateway may transcode to the provider format, but the client contract is stable. |

## Non-blocking subagent dispatch (realtime loop)

Subagent latency (seconds to tens of seconds) is incompatible with holding a live voice/text turn open, so the realtime loop dispatches `task` **fire-and-forget relative to the duplex session**, not relative to the subagent itself:

1. Model (via Gemini Live) emits a `task` tool call during a finalized utterance's post-processing, same as today's tool-call flow.
2. `run_realtime_turn()` hands the call to the existing subagent dispatch path (`core/subagents/subagent_proto.py` / `subagent_worker.py` / durable run queue if `MONKEYBOT_TASK_QUEUE=1`) — **reused as-is**, no new subagent execution code.
3. The realtime session is **not held open** waiting on the subagent. The model immediately continues the live conversation — either the model naturally produces a filler/ack utterance ("let me check on that") as part of its own turn, or the harness injects a short synthesized ack via the realtime provider's text-to-speech input if the model doesn't.
4. Subagent completion is delivered back **asynchronously**, out-of-band from the turn that spawned it:
   - On completion, the subagent result is queued for the session (mirrors `PendingResponseBus`/`bus.publish_data()` pattern from the SSE gateway, adapted for the duplex connection).
   - The realtime loop injects the result as a new synthetic "turn" only when the session reaches **true idle** — `listening` state, no active user utterance in progress. No max-wait timeout, and the agent never interrupts the user to deliver it. If the user is still talking when the result arrives, it simply waits.
   - If the connection has since closed, the result is persisted via the existing `RunStore` (`get(run_id)`) exactly as today — nothing is lost, it's just not delivered live.
5. History: the subagent's own turn/result commits to `HistoryStore` the same way it does in the turn-based loop today (no changes to subagent persistence semantics) — only the *delivery* mechanism back to the live user is new.

This keeps subagents fully reused from the existing harness (dispatch, execution, persistence, durable queue) — the only new piece is the "deliver result when session goes idle" injection point in the realtime loop, which is a natural fit for the `listening/thinking/speaking` state machine already scoped in Step 3.

---

## Open Questions

| Question | Notes |
|---|---|
| Multi-instance session affinity for realtime WS connections? | Same open problem as today's in-process `SessionRegistry` (`session_bus.py` comment: "use Redis pub/sub for multi-instance deployments"). Sticky load-balancer routing is the simplest v1 answer; do not build a distributed session bus speculatively. |
| Direct browser mic capture instead of server-to-server? | Would require WebRTC instead of WebSocket. Not a v1 use case; revisit if a client-side voice UI becomes first-class. |
| Provider-level token budgets or audio quotas? | Currently scoped to `max_duration_sec` and `max_response_turn_sec`. Per-minute audio caps or provider-native budget controls may be added later if cost becomes a concern. |

---

## Implementation Sequence

```
Step 0: Configuration & Packaging (DONE)
  - Add `harness.mode` and `realtime.*` schema to config loader/validator  DONE
  - Add `monkeybot[realtime]` and `monkeybot[realtime-gemini]` extras       DONE
  - Default mode is `turn_based`; existing configs remain valid            DONE

Step 1: RealtimeProvider Protocol (DONE — Gemini Live stubbed)
  - Define RealtimeProvider + RealtimeSession protocols                    DONE
  - Define RealtimeConfig, AudioFormat, RealtimeSessionConfig              DONE
  - Define RealtimeEvent union in core/llm/realtime_provider.py          DONE
  - Fake RealtimeSession test double                                       DONE
  - Gemini Live implementation ([realtime-gemini] extra)                   STUB — needs SDK wiring
  - Tests: protocol contract against fake + config validation              DONE

Step 2: Utterance Buffer & Turn-Boundary Detection (DONE — non-blocking task delivery stubbed)
  - core/runtime/utterance_buffer.py                                       DONE
  - core/runtime/realtime_loop.py with run_realtime_turn()                 DONE
  - run_realtime_turn() reuses tool executor, history, memory hooks        DONE
  - Interrupt handling (discard in-flight, no partial commit,              DONE
    keep user interrupt utterance)
  - Non-blocking task dispatch: reuse subagent_proto/worker/               STUB
    RunStore as-is; add session-idle delivery injection point            STUB
  - Tests: boundary detection, interrupt discards correctly,              DONE
    tool dispatch in run_realtime_turn()                                   DONE

Step 3: Realtime Gateway Transport (DONE — keepalive/idle timeout wiring for Step 4)
  - gateway/realtime/ package (new, sibling to gateway/sse/)                   DONE
  - WS /sessions/{id}/realtime route                                         DONE
  - Frame protocol (binary audio + JSON control) in wire.py                  DONE
  - Session state machine (listening/thinking/speaking/                      DONE
    interrupted/tool_running/closing) with transition table in session.py
  - Auth: gateway auth middleware does not exist today; v1 uses the            TODO (Step 4)
    session-id-in-path access control, pending an explicit auth decision
  - Concurrency guardrail in manager.py                                        DONE
  - Keepalive/idle timeout/max duration enforcement scheduled in loops       STUB (Step 4)
  - Tests: wire, session state machine, concurrency, app factory smoke           DONE
  - Tests: gateway/sse/ existing tests still pass unmodified                 DONE

Step 4: Operational Glue (DONE)
  - Observability: RealtimeMetrics + structured session summary on close     DONE
  - Error taxonomy and typed RealtimeError events in errors.py               DONE
  - Teardown: provider close, metrics emission, slot release in routes.py    DONE
  - Guardrails: max duration, idle timeout, max response turn in             DONE
    guardrails.py; concurrency limit returns HTTP 503 with Retry-After
  - Auth: still pending explicit gateway auth decision (no middleware today)  TODO (Step 5 / follow-up)

Step 5: Deployment Guides (DONE)
  - docs/deploy-pattern-d-realtime.md                                      DONE
      Addenda: Vertex AI Agent Engine, Bedrock AgentCore WS,
               self-hosted container
  - Entrypoint: src/monkeybot/gateway/realtime_main.py                     DONE
  - CLI client: src/monkeybot/cli/ with text + optional audio mode          DONE
  - Tests: smoke test for realtime entrypoint imports & routes              DONE
  - Tests: CLI argument parsing and client frame encoding                   DONE
```

---

## Backlog Items This Design Would Supersede

None yet — this is new scope, not listed in `BACKLOG.md`. Add an entry there under "Up Next" or "Do Later" once prioritized.
