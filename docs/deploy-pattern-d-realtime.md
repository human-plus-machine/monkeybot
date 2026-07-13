# Pattern D — Realtime WebSocket Deployment

Run MonkeyBot in realtime (full-duplex audio/text) mode alongside the existing SSE gateway. This pattern uses the WebSocket harness at `WS /sessions/{session_id}/realtime` while preserving the existing HTTP `/reply` + SSE path for turn-based clients.

Realtime is a **parallel control path** to turn-based `loop.run()` (duplex transport, audio, micro-turn session lifecycle). Use this guide for deploy/config; see [Features](features.md) for harness ownership boundaries and [SSE gateway](sse-gateway-ui.md) for the turn-based HTTP API.

**Use this pattern when:**

- You want a persistent, low-latency voice or streaming text conversation.
- You can accept v1 limitations: no reconnect/resume, one realtime session per WebSocket connection, and long-lived containers (Cloud Run scale-to-zero is not ideal for WebSocket).

**Targets covered:** GCP Cloud Run / GKE / GCE · AWS ECS / EKS / EC2 · Azure Container Apps / AKS / Azure VM · Vertex AI Agent Engine · Amazon Bedrock AgentCore (WebSocket) · self-hosted container

---

## 1. Required Configuration

Set `harness.mode` to `realtime` in `monkeybot.yaml`. Realtime settings are validated at startup even when the mode is `turn_based`, but they only take effect when `harness.mode` is `realtime`.

```yaml
harness:
  mode: realtime

realtime:
  websocket:
    enabled: true
    # port defaults to the runtime port when unset
  audio:
    input_format: pcm_s16le_24khz_mono
    output_format: pcm_s16le_24khz_mono
    chunk_ms: 200
  session:
    max_duration_sec: 1800
    idle_timeout_sec: 120
    max_response_turn_sec: 300
    max_concurrent_sessions: 100
  metrics:
    emit_summary_on_close: true
```

The model provider must be a Gemini-family provider for v1. Use `google_genai` for Google AI Studio (required for the `gemini-3.1-flash-live-preview` preview) or `google_vertexai` for Vertex AI:

```yaml
# Google AI Studio (required for gemini-3.1-flash-live-preview as of mid-2026)
model:
  provider: google_genai
  name: gemini-3.1-flash-live-preview

# Vertex AI
model:
  provider: google_vertexai
  name: gemini-2.5-flash
```

---

## 2. Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MODEL_PROVIDER` | Yes | `google_vertexai` | Must be `google_vertexai` or `google_genai` for realtime v1. |
| `MODEL_NAME` | Yes | `gemini-2.5-flash` | Model used for realtime. Context curation can use a separate curator model. |
| `GEMINI_API_KEY` | Yes (Google AI Studio) | — | API key when using `google_genai`. |
| `VERTEX_AI_PROJECT_ID` | Yes (Vertex) | — | GCP project for Vertex AI. |
| `VERTEX_AI_LOCATION` | No | `us-central1` | Vertex region. |
| `DB_URL` | No | `sqlite:////app/data/monkeybot.db` | Storage backend. Postgres or Firestore recommended for production. |
| `MEMORY_STORAGE_URI` | No | `local:///app/data/memory` | Workspace/memory backend. GCS/S3 for multi-instance. |
| `MCP_CONFIG` | No | `/app/monkeybot_config/mcp.json` | Path to MCP server config. |
| `LOG_LEVEL` | No | `info` | Logging verbosity. |
| `PORT` / `GATEWAY_PORT` | No | `8000` | Port the gateway listens on. |
| `GRACEFUL_SHUTDOWN_TIMEOUT_SEC` | No | `5` | Seconds to wait for open connections during shutdown. |

---

## 3. Install the Right Extras

Realtime needs the `realtime` base extra plus a vendor-specific extra. For Gemini Live:

```bash
# local development
pip install -e ".[realtime-gemini]"

# production container build (see Dockerfile example below)
# EXTRAS=realtime-gemini,postgres,gcs
```

`realtime` includes `websockets`. `realtime-gemini` includes `realtime` + `google-genai`.

Install the unified CLI (`monkeybot-cli` in `cli/`) plus harness extras for talk:

```bash
# Agent project (preferred): include extras in pyproject.toml, then uv sync
#   monkeybot[cli]            — text-only talk
#   monkeybot[cli-realtime]   — audio talk (requires PortAudio)

# Global CLI on PATH
uv tool install monkeybot-cli

# Harness contributors only (editable checkout):
#   uv sync --extra cli            # or cli-realtime
#   cd cli && uv tool install --editable .
```

---

## 4. Run Locally

```bash
# Ensure monkeybot.yaml is configured for realtime mode

# Google AI Studio (gemini-3.1-flash-live-preview)
export MODEL_PROVIDER=google_genai
export GEMINI_API_KEY=your-api-key

# Or Vertex AI
# export MODEL_PROVIDER=google_vertexai
# export VERTEX_AI_PROJECT_ID=your-project
# export VERTEX_AI_LOCATION=us-central1

export DB_URL=sqlite:////tmp/monkeybot.db
export MEMORY_STORAGE_URI=local:///tmp/monkeybot-memory
export PORT=8000

python -m monkeybot.gateway.realtime_main
```

Connect a client:

```javascript
const ws = new WebSocket("ws://localhost:8080/sessions/session-123/realtime");
ws.onopen = () => ws.send(JSON.stringify({ kind: "connect", session_id: "session-123" }));
ws.onmessage = (msg) => console.log(msg.data);
```

### CLI client

A bundled CLI client can connect to a running realtime gateway:

```bash
# Audio mode (default; requires PortAudio and `monkeybot[cli-realtime]`)
monkeybot talk

# Text-only mode (no microphone dependencies)
monkeybot talk --text

# Override defaults
monkeybot talk --gateway-url ws://localhost:8080 --session-id session-123
```

All arguments are optional and can be set via environment variables:

- `MONKEYBOT_GATEWAY_URL` — default `ws://127.0.0.1:8080` (or `runtime.port` from yaml)
- `MONKEYBOT_SESSION_ID` — auto-generated if not provided

In text mode, type messages and press Enter. The agent still responds through audio if your system can play it; transcripts are printed as well. Special commands:

- `/interrupt` — send an interrupt signal
- `/quit` — close the session and exit

In default mode, the CLI streams microphone input and plays model audio through the speakers. Text commands still work.

The same process also serves the existing SSE routes (`POST /sessions`, `POST /reply`, etc.) so turn-based clients can coexist.

---

## 5. Build and Deploy the Container

```bash
# Base realtime image with Gemini Live
docker build -f docker/Dockerfile --build-arg EXTRAS=realtime-gemini -t monkeybot:realtime .

# With managed Postgres and GCS memory
docker build -f docker/Dockerfile --build-arg EXTRAS=realtime-gemini,postgres,gcs -t monkeybot:realtime .
```

Override the container command to start the realtime entrypoint:

```bash
docker run -p 8000:8000 \
  -e MODEL_PROVIDER=google_vertexai \
  -e VERTEX_AI_PROJECT_ID=your-project \
  -e DB_URL=postgresql://... \
  -e MEMORY_STORAGE_URI=gcs://your-bucket/monkeybot-memory \
  -e harness__mode=realtime \
  monkeybot:realtime \
  python -m monkeybot.gateway.realtime_main
```

**Note:** The Dockerfile's default `CMD` is `python -m monkeybot.gateway.main` (SSE-only). For realtime deployments, override the command to `python -m monkeybot.gateway.realtime_main`.

---

## 6. Load Balancing and Sticky Sessions

Realtime session state is in-memory and single-process. Multi-instance deployments require sticky routing so a client's WebSocket reconnects (on refresh) land on the same pod:

- **GKE / EKS / AKS:** Use session-affinity on the Ingress or Service.
- **Cloud Run:** Use [session affinity](https://cloud.google.com/run/docs/configuring/session-affinity) (WebSocket connections are kept on the same instance for the lifetime of the connection). v1 does not support reconnect/resume; a new session starts a new provider connection.
- **ECS / Container Apps:** Use cookie-based or source-IP affinity on the load balancer.

---

## 7. Scaling and Cost Considerations

- **Realtime sessions are long-lived.** Avoid scale-to-zero platforms if you expect sustained voice traffic. Minimum instance count > 0 is recommended for Cloud Run / Container Apps.
- **Concurrency is bounded** by `realtime.session.max_concurrent_sessions`. Excess WebSocket upgrades are rejected with HTTP `503` and `Retry-After: 10` before the upgrade is accepted.
- **Idle timeout** (`realtime.session.idle_timeout_sec`) and **max duration** (`realtime.session.max_duration_sec`) protect against abandoned or runaway sessions.
- **Max response turn** (`realtime.session.max_response_turn_sec`) prevents a single model monologue from holding a session indefinitely.

---

## 8. Observability

Realtime sessions emit structured logs and a session summary on close:

```json
{
  "event": "realtime_session_summary",
  "session_id": "...",
  "request_id": "...",
  "realtime_session_duration_sec": 120.5,
  "realtime_session_user_audio_sec": 45.2,
  "realtime_session_model_audio_sec": 38.1,
  "realtime_session_interrupt_count": 2,
  "realtime_session_turn_count": 6,
  "realtime_session_close_reason": "client_disconnect"
}
```

Export these logs to your monitoring stack (Cloud Logging, Datadog, etc.) the same way you export existing MonkeyBot logs.

---

## Per-Target Addenda

### Vertex AI Agent Engine

Vertex AI Agent Engine provides managed Gemini Live (`bidi_stream_query`) endpoints. The MonkeyBot `GeminiLiveProvider` uses the same `google-genai` SDK as the turn-based Vertex provider.

1. Build with `realtime-gemini` extra.
2. Set `MODEL_PROVIDER=google_vertexai` and `VERTEX_AI_PROJECT_ID`.
3. The gateway calls `client.aio.live.connect(...)` using the Vertex project/location inferred from `VERTEX_AI_PROJECT_ID` and `VERTEX_AI_LOCATION`.
4. Ensure the runtime service account has `roles/aiplatform.user`.

**Note:** `gemini-3.1-flash-live-preview` is not available on Vertex AI as of mid-2026; use `google_genai` (Google AI Studio) for that model.

### Google AI Studio

Google AI Studio is the only surface that hosts the `gemini-3.1-flash-live-preview` preview model as of mid-2026.

1. Build with `realtime-gemini` extra.
2. Set `MODEL_PROVIDER=google_genai` and `GEMINI_API_KEY`.
3. The gateway calls `client.aio.live.connect(...)` with `api_key=GEMINI_API_KEY`.
4. For production, use [ephemeral tokens](https://ai.google.dev/gemini-api/docs/live-api/session-management) so the API key does not travel to clients.

### Amazon Bedrock AgentCore (WebSocket)

Bedrock AgentCore exposes a managed WebSocket at `wss://...`. For v1, MonkeyBot does not include a Bedrock `RealtimeProvider` implementation. To add one:

1. Implement `RealtimeProvider` and `RealtimeSession` in `providers/bedrock_live.py`.
2. Map Bedrock Nova Sonic events to the `RealtimeEvent` union.
3. Build with a new `realtime-bedrock` extra (not included in v1).

Until then, run Pattern D on a self-managed container in AWS (ECS/EKS/EC2) and use the Gemini Live provider.

### Self-Hosted Container

The simplest production deployment for Pattern D is a long-lived container behind a TCP load balancer with session affinity.

**Recommended stack:**

- Container: `monkeybot:realtime` with `realtime-gemini,postgres,gcs` extras.
- Database: managed Postgres for `HistoryStore` and `RunStore`.
- Memory: GCS or S3 object storage for `MEMORY_STORAGE_URI`.
- Load balancer: TCP/SSL passthrough with session affinity (cookie or source IP).
- Autoscaling: target CPU/memory, but keep a minimum instance count > 0 for low latency.

**Health checks:** use the existing SSE health/readiness endpoint (`GET /api/health` or similar) from the SSE gateway, which is also mounted by the realtime app.

**Shutdown:** set `GRACEFUL_SHUTDOWN_TIMEOUT_SEC` to allow open WebSocket connections to close cleanly. The provider session is closed and a final metric summary is emitted.

---

## 9. Migration from Turn-Based (Pattern A/B/C)

Pattern D is additive. You can run the realtime app alongside the existing SSE app:

- Keep turn-based clients on `monkeybot.gateway.main` (default SSE entrypoint).
- Route realtime clients to `monkeybot.gateway.realtime_main`.
- Both entrypoints share the same `monkeybot.yaml`; only `harness.mode` differs.
- Both write to the same `HistoryStore` and `RunStore` so conversation history is portable across modes.

For a single process that serves both, use `monkeybot.gateway.realtime_main` — it mounts the SSE routes unchanged.

---

## 10. Known Limitations and v1 Decisions

- **No reconnect/resume.** A WebSocket disconnect ends the realtime session. The client creates a new session and reconnects.
- **No auth middleware.** v1 uses the session ID in the URL as access control. Add an explicit auth check before `await ws.accept()` once a gateway auth middleware is decided.
- **Gemini Live only.** The `RealtimeProvider` protocol is vendor-neutral, but v1 only has a Gemini Live adapter.
- **No mid-tool interruption.** Once a tool call is dispatched, it runs to completion. This is a permanent design decision.
