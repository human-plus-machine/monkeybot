# Playground Chat UI

Vite + React dev client for the local MonkeyBot SSE gateway. Use it to drive an agent from a browser while iterating on `AGENT.md`, skills, or the loop.

## Run

From the repo root, start the gateway in one terminal:

```bash
cd playground/agent
cp .env.example .env   # first time only
uv sync
./run.sh
```

Then start the UI in another terminal:

```bash
cd playground/chat-ui
npm install            # first time only
npm run dev
```

Open the URL Vite prints (usually `http://localhost:5173`).

## Gateway target

The dev server proxies API calls under `/__mb_gateway` to the gateway. The default target is `http://127.0.0.1:8787` (matches the gateway port in `playground/agent/.env.example`).

To point at a gateway running elsewhere, copy `env.local.sample` to `.env.local` and set:

```
VITE_GATEWAY_TARGET=http://127.0.0.1:8080
```

## See also

- Repo-level overview: [`README.md`](../../README.md) (Playground section)
- Gateway runner: [`playground/agent/`](../agent/)
- HTTP API: `monkeybot.gateway.sse.routes` (`POST /sessions`, `GET /sessions/{id}/events`, `POST /sessions/{id}/reply`, `GET /health`)
