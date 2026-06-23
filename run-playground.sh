#!/usr/bin/env bash
# Start playground backend (gateway + Docker deps) and chat UI from the repo root.
#
# Usage:
#   ./run-playground.sh
#
# Environment (passed through to playground/agent/run.sh):
#   SKIP_OPENSANDBOX=1          Skip OpenSandbox Docker container
#   SKIP_OBSERVABILITY=1        Skip Phoenix/Langfuse stack
#   PRESERVE_OPENSANDBOX=1      Leave OpenSandbox running after exit
#   PRESERVE_OBSERVABILITY=1    Leave observability stack running after exit
#   GATEWAY_PORT=8787           Health-check port (default matches monkeybot.yaml)
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="${ROOT}/playground/agent"
UI_DIR="${ROOT}/playground/chat-ui"
GATEWAY_PORT="${GATEWAY_PORT:-8787}"
GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}"
GATEWAY_WAIT_SECS="${GATEWAY_WAIT_SECS:-90}"

BACKEND_PID=""
FRONTEND_PID=""
_CLEANUP_DONE=0

log() {
  printf 'run-playground: %s\n' "$*"
}

kill_tree() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  if command -v pkill >/dev/null 2>&1; then
    pkill -TERM -P "$pid" 2>/dev/null || true
  fi
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  if [[ "$_CLEANUP_DONE" == "1" ]]; then
    return 0
  fi
  _CLEANUP_DONE=1
  log "shutting down…"
  if [[ -n "$FRONTEND_PID" ]]; then
    kill_tree "$FRONTEND_PID"
  fi
  if [[ -n "$BACKEND_PID" ]]; then
    kill_tree "$BACKEND_PID"
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

if [[ ! -x "${AGENT_DIR}/run.sh" ]]; then
  log "missing ${AGENT_DIR}/run.sh"
  exit 1
fi
if [[ ! -f "${UI_DIR}/package.json" ]]; then
  log "missing ${UI_DIR}/package.json"
  exit 1
fi

if [[ ! -d "${UI_DIR}/node_modules" ]]; then
  log "installing chat-ui dependencies (first run)…"
  if command -v pnpm >/dev/null 2>&1 && [[ -f "${UI_DIR}/pnpm-lock.yaml" ]]; then
    (cd "$UI_DIR" && pnpm install)
  else
    (cd "$UI_DIR" && npm install)
  fi
fi

log "starting gateway (playground/agent/run.sh)…"
(
  cd "$AGENT_DIR"
  ./run.sh
) &
BACKEND_PID=$!

wait_for_gateway() {
  if ! command -v curl >/dev/null 2>&1; then
    log "curl not found; waiting 5s before starting UI"
    sleep 5
    return 0
  fi
  local max=$((GATEWAY_WAIT_SECS * 2))
  local i
  for ((i = 1; i <= max; i++)); do
    if curl -4sSf "${GATEWAY_URL}/health" >/dev/null 2>&1; then
      log "gateway ready at ${GATEWAY_URL}"
      return 0
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      log "gateway process exited before /health responded"
      exit 1
    fi
    sleep 0.5
  done
  log "WARNING — gateway not healthy after ${GATEWAY_WAIT_SECS}s; starting UI anyway"
  log "check [gateway] logs above; gateway may still be pulling Docker images"
}

wait_for_gateway

log "starting chat UI (Vite dev server)…"
log "open http://localhost:5173 when Vite prints the URL"
(
  cd "$UI_DIR"
  if command -v pnpm >/dev/null 2>&1 && [[ -f pnpm-lock.yaml ]]; then
    pnpm run dev
  else
    npm run dev
  fi
) &
FRONTEND_PID=$!

wait "$FRONTEND_PID" 2>/dev/null || true
exit 0
