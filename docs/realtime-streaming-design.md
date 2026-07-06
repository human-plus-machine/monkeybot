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

## Non-Negotiable Design Constraints

1. **`loop.run()` and the existing `/reply` + SSE path are not modified.** This is additive architecture. Existing turn-based deployments (Pattern A/B/C) must see zero behavior change.
2. **No new global singletons.** The realtime session state machine lives per-`SessionBus`-equivalent object, analogous to today's `SessionRegistry` (in-memory, single-process; multi-instance requires an external pub/sub, same caveat already noted in `session_bus.py`).
3. **Utterance-boundary commits only.** Partial/in-flight audio or text is never written to `HistoryStore`. History still only ever contains complete, finalized `Message` rows — this preserves every existing invariant in `docs/features.md` (`ToolRequest`/`ToolResponse` pairing, tool-call ordering, summarization, memory hooks) without touching them.
4. **Tool execution is post-utterance only; no mid-tool-execution interruption.** Tools run after an utterance is finalized (VAD/endpoint signal), same as today's "after `Done`" semantics — just triggered by an endpoint event instead of a provider stream end. Once a tool call is dispatched, the user cannot interrupt it — this is a permanent design decision, not a v1 limitation to revisit later.
5. **Realtime provider is a new, separate protocol — not a `stream()` shape.** `Provider.stream()` is request/response-per-call; realtime vendor APIs (Gemini Live, OpenAI Realtime, Bedrock Nova Sonic) are persistent duplex sessions. Do not force-fit them into the existing `Provider` protocol. Define a new `RealtimeProvider` protocol instead.
6. **Transport is WebSocket. Not WebRTC.** MonkeyBot's target deployments (Cloud Run/ECS server-to-server, Vertex AI Agent Engine `bidi_stream_query`, Bedrock AgentCore `/ws`) are all WebSocket-native. WebRTC only pays off for direct-from-browser mic capture over lossy networks, which is not a MonkeyBot use case.
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
class RealtimeProvider(Protocol):
    async def connect(self, *, model: str, system_prompt: str, tools: Sequence[ToolDef]) -> RealtimeSession: ...

class RealtimeSession(Protocol):
    async def send_audio(self, chunk: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    def events(self) -> AsyncIterator[RealtimeEvent]: ...  # PartialTranscript, AudioDelta, TurnBoundary, ToolCall, Done
    async def close(self) -> None: ...
```

- `RealtimeEvent` union lives in `core/runtime/events.py` alongside existing `AgentEvent` types, tagged distinctly (e.g. `RealtimePartialTranscript`, `RealtimeAudioDelta`, `RealtimeTurnBoundary`) so gateway code can share the SSE/WS event-serialization utilities already in `gateway/sse/sse.py`.

### First implementation — decided: Gemini Live

Matches existing `google_vertexai` provider family and Vertex AI Agent Engine's native `bidi_stream_query` pattern. Gated behind `monkeybot[realtime-gemini]` extra (`websockets` dependency, no new heavy SDKs by default). OpenAI Realtime / Bedrock Nova Sonic remain future `RealtimeProvider` implementations behind their own extras, not required for v1.

### Tests

- Fake `RealtimeSession` double (mirrors `ScriptedFakeProvider` pattern in `core/testing/mocks_provider.py`) for deterministic unit tests without a live vendor session.

---

## Step 2: Utterance Buffer & Turn-Boundary Detection

**Goal:** Bridge continuous input chunks to the existing message-granular `HistoryStore` without touching its schema or invariants.

### What changes

New module `core/runtime/utterance_buffer.py`:
- Accumulates incoming audio/text chunks per session in memory only.
- Detects turn boundaries via the realtime provider's own endpointing (Gemini Live, OpenAI Realtime, and Nova Sonic all emit end-of-turn / speech-stopped signals — reuse vendor VAD rather than building a custom one for v1).
- On boundary: hands the finalized utterance to a new `run_realtime_turn()` entry point that mirrors `loop.run()`'s post-utterance behavior (tool dispatch, history append, memory hooks) but is driven by a boundary event, not an HTTP POST.
- On interrupt (user starts speaking while agent is speaking): discards in-flight output, does **not** partially commit history, emits an `Interrupted` event.
- `task` calls dispatch through the existing subagent path without blocking the duplex session; completion is delivered back on next session-idle (see "Non-blocking subagent dispatch" below).

### What does NOT change

- `HistoryStore`, `Message`, `ContentBlock` — untouched. Finalized utterances become ordinary `Message` rows exactly as today.
- `core/tools/core_tool_executor.py`, `core/subagents/subagent_proto.py`, `subagent_worker.py`, `RunStore` — all reused as-is once an utterance is finalized.

---

## Step 3: Realtime Gateway Transport

**Goal:** New WebSocket route, isolated from the existing SSE routes.

### What changes

- New route: `WS /sessions/{session_id}/realtime` in a new `gateway/realtime/` package (sibling to `gateway/sse/`, not inside it — keeps the existing SSE code path untouched and independently testable).
- Frame protocol (binary audio chunks + JSON control/text frames, similar to AgentCore's `/ws` and Gemini Live's client protocol).
- Session state machine per connection: `listening → thinking → speaking`, with `interrupted` as a transition from `speaking`/`thinking` back to `listening`.
- Reuses `SessionRegistry`-style per-process session tracking; explicitly does **not** reuse `session_turn_locks.py` (that lock models "one exclusive HTTP turn," which doesn't apply to a persistent duplex connection).

### What does NOT change

- `gateway/sse/routes.py`, `session_bus.py`, `app.py` — zero modifications. Existing `/reply` + SSE clients are unaffected.

---

## Step 4: Deployment Guides

**Goal:** Document the two platform-native realtime targets, following the existing Pattern A/B/C guide structure in `docs/deploy-pattern-*.md`.

New addenda (not new pattern documents — these slot into a **Pattern D: Realtime/Bidirectional** doc, since neither existing pattern's request/response or short-lived-invocation model fits a persistent duplex session):

**`docs/deploy-pattern-d-realtime.md`**
- GCP Vertex AI Agent Engine: `bidi_stream_query`, `agent_server_mode=EXPERIMENTAL`, ADK's `AdkApp` auto-wiring vs. a thin custom adapter over `run_realtime_turn()`.
- AWS Bedrock AgentCore Runtime: WebSocket `/ws` endpoint (SigV4/OAuth2 auth), contrasted with the existing HTTP `/invocations` contract in `docs/deploy-aws-agentcore.md`.
- Self-hosted container (Pattern A style): same `docker/Dockerfile` image, additional `realtime` extras, new `WS_ENABLED=1`-style env flag to mount the route.

---

## Decisions

| Question | Decision |
|---|---|
| Which realtime vendor first — Gemini Live or OpenAI Realtime? | **Gemini Live.** Aligns with existing `google_vertexai` provider investment and Agent Engine's native `bidi_stream_query` support. |
| Does `task`/subagent dispatch make sense mid-conversation? | **Yes, non-blocking.** Main realtime loop dispatches `task` the same way `loop.run()` does today, but does not block the duplex session waiting on it — see "Non-blocking subagent dispatch" below. |
| When does a completed subagent result get delivered if the user is still talking? | **Only at true session-idle.** No max-wait timeout, no interrupting the user. The result waits in `RunStore` until the session state machine reaches `listening` with no active utterance in progress. |

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

## Open Questions

| Question | Notes |
|---|---|
| Multi-instance session affinity for realtime WS connections? | Same open problem as today's in-process `SessionRegistry` (`session_bus.py` comment: "use Redis pub/sub for multi-instance deployments"). Sticky load-balancer routing is the simplest v1 answer; do not build a distributed session bus speculatively. |

---

## Implementation Sequence

```
Step 1: RealtimeProvider Protocol
  - Define RealtimeProvider + RealtimeSession protocols       TODO
  - Define RealtimeEvent union in core/runtime/events.py      TODO
  - Fake RealtimeSession test double                          TODO
  - Gemini Live implementation ([realtime-gemini] extra)      TODO
  - Tests: protocol contract against fake + Gemini Live       TODO

Step 2: Utterance Buffer & Turn-Boundary Detection
  - core/runtime/utterance_buffer.py                          TODO
  - run_realtime_turn() entry point (reuses tool executor,    TODO
    history, memory hooks post-utterance)
  - Interrupt handling (discard in-flight, no partial commit) TODO
  - Non-blocking task dispatch: reuse subagent_proto/worker/  TODO
    RunStore as-is; add session-idle delivery injection point
  - Tests: boundary detection, interrupt discards correctly,  TODO
    subagent result delivered only at session-idle

Step 3: Realtime Gateway Transport
  - gateway/realtime/ package (new, sibling to gateway/sse/)  TODO
  - WS /sessions/{id}/realtime route                          TODO
  - Frame protocol (binary audio + JSON control)              TODO
  - Session state machine (listening/thinking/speaking)       TODO
  - Tests: gateway/sse/ existing tests still pass unmodified  TODO

Step 4: Deployment Guides
  - docs/deploy-pattern-d-realtime.md                         TODO
      Addenda: Vertex AI Agent Engine, Bedrock AgentCore WS,
               self-hosted container
```

---

## Backlog Items This Design Would Supersede

None yet — this is new scope, not listed in `BACKLOG.md`. Add an entry there under "Up Next" or "Do Later" once prioritized.
