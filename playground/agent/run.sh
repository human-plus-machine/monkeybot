#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# OpenSandbox API (matches monkeybot_config/monkeybot.yaml sandbox.server_url default).
# Set SKIP_OPENSANDBOX=1 to skip. Requires Docker; sandboxes need the host socket.
SANDBOX_CONTAINER="${SANDBOX_CONTAINER:-monkeybot-playground-opensandbox}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-opensandbox/server:latest}"
SANDBOX_HOST_PORT="${SANDBOX_HOST_PORT:-18080}"
# Docker runtime + bindable API (see monkeybot_config/opensandbox.docker.toml).
SANDBOX_CONFIG_HOST="${SANDBOX_CONFIG_HOST:-$(pwd)/monkeybot_config/opensandbox.docker.toml}"
# Seconds to wait for OpenSandbox /health after docker start/run (server pulls images on first boot).
SANDBOX_HEALTH_WAIT_SECS="${SANDBOX_HEALTH_WAIT_SECS:-5}"

# Firestore emulator (only when db_url uses firestore://). Set SKIP_FIRESTORE_EMULATOR=1 to skip.
FIRESTORE_CONTAINER="${FIRESTORE_CONTAINER:-monkeybot-playground-firestore}"
FIRESTORE_EMULATOR_HOST_PORT="${FIRESTORE_EMULATOR_HOST_PORT:-8686}"
FIRESTORE_PROJECT="${FIRESTORE_PROJECT:-monkeybot-playground}"
# Official Google Cloud SDK emulators image (566.0.0-emulators, pinned by digest).
FIRESTORE_EMULATOR_IMAGE="${FIRESTORE_EMULATOR_IMAGE:-gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:b19eb965d67981489383d544d12283b806040fb13e99cccfdbbdf4c818c2f2ab}"
FIRESTORE_HEALTH_WAIT_SECS="${FIRESTORE_HEALTH_WAIT_SECS:-15}"
_FIRESTORE_EMULATOR_PID=""

_opensandbox_health_url() {
  echo "http://127.0.0.1:${SANDBOX_HOST_PORT}/health"
}

_opensandbox_health_body() {
  # -4: avoid rare IPv6/IPv4 split where another process owns one family on the same port.
  curl -4sS --connect-timeout 2 --max-time 5 "$(_opensandbox_health_url)" 2>/dev/null || true
}

# Hash of OpenSandbox TOML — recreate the server container when it changes (otherwise the
# daemon keeps an old in-memory allowlist and bind mounts fail with Allowed prefixes: []).
_opensandbox_config_sha256() {
  if [[ ! -f "${SANDBOX_CONFIG_HOST}" ]]; then
    echo ""
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${SANDBOX_CONFIG_HOST}" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${SANDBOX_CONFIG_HOST}" | awk '{print $1}'
  else
    # Fallback: size+mtime so edits still bump the "version"
    printf '%s-%s' "$(wc -c <"${SANDBOX_CONFIG_HOST}" 2>/dev/null || echo 0)" "$(stat -f '%m' "${SANDBOX_CONFIG_HOST}" 2>/dev/null || stat -c '%Y' "${SANDBOX_CONFIG_HOST}" 2>/dev/null || echo 0)"
  fi
}

# Returns 0 if OpenSandbox responds on SANDBOX_HOST_PORT; 1 otherwise.
opensandbox_health_ok() {
  if [[ "${SKIP_OPENSANDBOX:-}" == "1" ]] || ! command -v curl >/dev/null 2>&1; then
    return 0
  fi
  if [[ ! -f "${SANDBOX_CONFIG_HOST}" ]]; then
    return 0
  fi
  local body
  body=$(_opensandbox_health_body)
  [[ "$body" == *"\"status\""* && "$body" == *"healthy"* ]]
}

verify_opensandbox_health() {
  if opensandbox_health_ok; then
    echo "run.sh: OpenSandbox health OK ($(_opensandbox_health_url))"
    return 0
  fi
  if [[ "${SKIP_OPENSANDBOX:-}" == "1" ]] || ! command -v curl >/dev/null 2>&1 || [[ ! -f "${SANDBOX_CONFIG_HOST}" ]]; then
    return 0
  fi
  local body
  body=$(_opensandbox_health_body)
  echo "run.sh: WARNING — $(_opensandbox_health_url) did not return OpenSandbox JSON."
  echo "run.sh: Response (first 200 chars): ${body:0:200}"
  echo "run.sh: If you see plain \"Not Found\", host port ${SANDBOX_HOST_PORT} is likely not this container (stale run without -p, or another app)."
  echo "run.sh: Inspect: lsof -nP -iTCP:${SANDBOX_HOST_PORT} -sTCP:LISTEN"
  echo "run.sh: Or use a free port: SANDBOX_HOST_PORT=28080 (and set sandbox.server_url to match in monkeybot.yaml)."
  return 1
}

# Poll until /health returns JSON or timeout. Returns 0 on success, 1 on failure.
wait_opensandbox_ready() {
  if [[ "${SKIP_OPENSANDBOX:-}" == "1" ]] || ! command -v curl >/dev/null 2>&1 || [[ ! -f "${SANDBOX_CONFIG_HOST}" ]]; then
    return 0
  fi
  local max=$((SANDBOX_HEALTH_WAIT_SECS * 2))
  local i st
  for ((i = 1; i <= max; i++)); do
    if opensandbox_health_ok; then
      echo "run.sh: OpenSandbox health OK ($(_opensandbox_health_url))"
      return 0
    fi
    if docker container inspect "${SANDBOX_CONTAINER}" >/dev/null 2>&1; then
      st=$(docker inspect -f '{{.State.Status}}' "${SANDBOX_CONTAINER}" 2>/dev/null || echo unknown)
      if [[ "$st" == "exited" ]] || [[ "$st" == "dead" ]]; then
        echo "run.sh: OpenSandbox container exited early (status=${st}). Recent logs:"
        docker logs --tail 50 "${SANDBOX_CONTAINER}" 2>&1 || true
        verify_opensandbox_health || true
        return 1
      fi
    fi
    sleep 0.5
  done
  echo "run.sh: WARNING — OpenSandbox did not become healthy within ${SANDBOX_HEALTH_WAIT_SECS}s."
  verify_opensandbox_health || true
  if docker container inspect "${SANDBOX_CONTAINER}" >/dev/null 2>&1; then
    echo "run.sh: OpenSandbox container logs (last 50 lines):"
    docker logs --tail 50 "${SANDBOX_CONTAINER}" 2>&1 || true
  fi
  return 1
}

# Set PRESERVE_OPENSANDBOX=1 to leave the container running after the gateway exits.
_run_sh_cleanup() {
  if [[ "${_RUN_SH_CLEANUP_DONE:-}" == "1" ]]; then
    return 0
  fi
  _RUN_SH_CLEANUP_DONE=1
  if [[ "${PRESERVE_OPENSANDBOX:-}" == "1" ]] || [[ "${SKIP_OPENSANDBOX:-}" == "1" ]]; then
    return 0
  fi
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  if ! docker container inspect "${SANDBOX_CONTAINER}" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$(docker container inspect -f '{{.State.Running}}' "${SANDBOX_CONTAINER}" 2>/dev/null)" == "true" ]]; then
    echo "run.sh: stopping OpenSandbox container ${SANDBOX_CONTAINER}"
    docker stop -t 10 "${SANDBOX_CONTAINER}" >/dev/null 2>&1 || true
  fi
}

# Observability stack (Phoenix + Langfuse + OTel Collector). Set SKIP_OBSERVABILITY=1 to skip.
OBS_LANGFUSE_COMPOSE="${OBS_LANGFUSE_COMPOSE:-$(pwd)/docker-compose.langfuse.yml}"
OBS_STACK_COMPOSE="${OBS_STACK_COMPOSE:-$(pwd)/docker-compose.observability.yml}"
OBS_COLLECTOR_CONFIG_PHOENIX="${OBS_COLLECTOR_CONFIG_PHOENIX:-$(pwd)/monkeybot_config/otel-collector.playground.yaml}"
OBS_COLLECTOR_CONFIG_DUAL="${OBS_COLLECTOR_CONFIG_DUAL:-$(pwd)/monkeybot_config/otel-collector.playground-dual.yaml}"
# How long to block gateway startup waiting for Phoenix + OTLP collector (not Langfuse).
OBS_HEALTH_WAIT_SECS="${OBS_HEALTH_WAIT_SECS:-45}"
# Langfuse migrations can take several minutes; we never block the gateway on this.
OBS_LANGFUSE_WAIT_SECS="${OBS_LANGFUSE_WAIT_SECS:-300}"

_obs_compose() {
  docker compose -f "${OBS_LANGFUSE_COMPOSE}" -f "${OBS_STACK_COMPOSE}" "$@"
}

_phoenix_health_ok() {
  command -v curl >/dev/null 2>&1 || return 0
  local code
  code=$(curl -4sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 "http://127.0.0.1:6006/" 2>/dev/null || echo "000")
  [[ "$code" =~ ^[23] ]]
}

_langfuse_health_ok() {
  command -v curl >/dev/null 2>&1 || return 0
  local code
  code=$(curl -4sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 8 "http://127.0.0.1:3000/api/public/health" 2>/dev/null || echo "000")
  [[ "$code" == "200" ]]
}

_collector_port_open() {
  command -v curl >/dev/null 2>&1 || return 0
  curl -4sS --connect-timeout 1 --max-time 2 "http://127.0.0.1:4318/" >/dev/null 2>&1
  return 0
}

wait_observability_ready() {
  local max=$((OBS_HEALTH_WAIT_SECS * 2))
  local i
  local last_log=0
  for ((i = 1; i <= max; i++)); do
    if _phoenix_health_ok && _collector_port_open; then
      echo "run.sh: Phoenix + OTLP collector ready (:6006, :4318)"
      if _langfuse_health_ok; then
        echo "run.sh: Langfuse ready at http://localhost:3000"
      else
        echo "run.sh: Langfuse still starting (gateway will start now; UI may take a few minutes)"
        echo "run.sh: Check http://localhost:3000 — first boot often needs ${OBS_LANGFUSE_WAIT_SECS}s+"
      fi
      return 0
    fi
    if (( i - last_log >= 20 )); then
      echo "run.sh: waiting for Phoenix/OTLP collector… (Langfuse containers may still be migrating)"
      last_log=$i
    fi
    sleep 0.5
  done
  echo "run.sh: WARNING — Phoenix/OTLP not ready within ${OBS_HEALTH_WAIT_SECS}s; starting gateway anyway."
  echo "run.sh: Phoenix: $(_phoenix_health_ok && echo OK || echo pending)"
  echo "run.sh: OTLP :4318: $(_collector_port_open && echo reachable || echo pending)"
  echo "run.sh: Langfuse: $(_langfuse_health_ok && echo OK || echo pending)"
  return 0
}

_observability_cleanup() {
  if [[ "${PRESERVE_OBSERVABILITY:-}" == "1" ]] || [[ "${SKIP_OBSERVABILITY:-}" == "1" ]]; then
    return 0
  fi
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  echo "run.sh: stopping observability stack (Phoenix, Langfuse, collector)"
  _obs_compose down >/dev/null 2>&1 || true
}

ensure_observability_stack() {
  if [[ "${SKIP_OBSERVABILITY:-}" == "1" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "run.sh: docker not found; skipping observability stack (SKIP_OBSERVABILITY=1 to silence)."
    return 0
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "run.sh: Docker daemon not reachable; skipping observability stack."
    echo "run.sh: Start Docker Desktop (or the daemon), then re-run."
    return 0
  fi
  if [[ ! -f "${OBS_LANGFUSE_COMPOSE}" ]] || [[ ! -f "${OBS_STACK_COMPOSE}" ]]; then
    echo "run.sh: missing observability compose files; skipping."
    return 0
  fi

  local collector_cfg="${OBS_COLLECTOR_CONFIG_PHOENIX}"
  if [[ -n "${LANGFUSE_OTEL_BASIC_AUTH:-}" ]] && [[ -f "${OBS_COLLECTOR_CONFIG_DUAL}" ]]; then
    collector_cfg="${OBS_COLLECTOR_CONFIG_DUAL}"
    echo "run.sh: OTel collector dual export (Phoenix + Langfuse)"
  else
    echo "run.sh: OTel collector → Phoenix only (set LANGFUSE_OTEL_BASIC_AUTH for Langfuse export)"
  fi
  export OTEL_COLLECTOR_CONFIG_HOST="${collector_cfg}"

  echo "run.sh: starting observability stack (Langfuse may take a few minutes on first run)…"
  if ! _obs_compose up -d; then
    echo "run.sh: docker compose up failed for observability (ports 3000/4318/6006 may be in use)."
    return 0
  fi
  wait_observability_ready

  export MONKEYBOT_OTEL_ENABLED="${MONKEYBOT_OTEL_ENABLED:-true}"
  export OTEL_TRACES_EXPORTER="${OTEL_TRACES_EXPORTER:-otlp}"
  export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}"
  export OTEL_METRICS_EXPORTER="${OTEL_METRICS_EXPORTER:-none}"
  export OTEL_LOGS_EXPORTER="${OTEL_LOGS_EXPORTER:-none}"
  export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-monkeybot-gateway}"
}

_playground_db_url() {
  if [[ -n "${DB_URL:-}" ]]; then
    printf '%s' "${DB_URL}"
    return 0
  fi
  grep -E '^\s*db_url:' monkeybot_config/monkeybot.yaml 2>/dev/null \
    | head -1 \
    | sed -E 's/^[[:space:]]*db_url:[[:space:]]*//' \
    | tr -d "\"'" \
    || true
}

_playground_uses_firestore() {
  local db_url
  db_url="$(_playground_db_url)"
  [[ "$db_url" == firestore://* ]]
}

_playground_uses_sqlite() {
  local db_url
  db_url="$(_playground_db_url)"
  [[ "$db_url" == sqlite://* ]]
}

_firestore_emulator_health_ok() {
  if [[ "${SKIP_FIRESTORE_EMULATOR:-}" == "1" ]]; then
    return 0
  fi
  command -v curl >/dev/null 2>&1 || return 0
  curl -4sS --connect-timeout 1 --max-time 3 \
    "http://127.0.0.1:${FIRESTORE_EMULATOR_HOST_PORT}/" >/dev/null 2>&1
}

wait_firestore_emulator_ready() {
  if [[ "${SKIP_FIRESTORE_EMULATOR:-}" == "1" ]]; then
    return 0
  fi
  local max=$((FIRESTORE_HEALTH_WAIT_SECS * 2))
  local i
  for ((i = 1; i <= max; i++)); do
    if _firestore_emulator_health_ok; then
      echo "run.sh: Firestore emulator ready (127.0.0.1:${FIRESTORE_EMULATOR_HOST_PORT})"
      return 0
    fi
    sleep 0.5
  done
  echo "run.sh: WARNING — Firestore emulator did not become ready within ${FIRESTORE_HEALTH_WAIT_SECS}s."
  return 1
}

_start_firestore_emulator_gcloud() {
  if ! command -v gcloud >/dev/null 2>&1; then
    return 1
  fi
  echo "run.sh: starting Firestore emulator via gcloud (127.0.0.1:${FIRESTORE_EMULATOR_HOST_PORT})…"
  gcloud emulators firestore start --host-port="127.0.0.1:${FIRESTORE_EMULATOR_HOST_PORT}" >/tmp/monkeybot-firestore-emulator.log 2>&1 &
  _FIRESTORE_EMULATOR_PID=$!
  wait_firestore_emulator_ready
}

_start_firestore_emulator_firebase() {
  if ! command -v firebase >/dev/null 2>&1; then
    return 1
  fi
  echo "run.sh: starting Firestore emulator via firebase (127.0.0.1:${FIRESTORE_EMULATOR_HOST_PORT})…"
  firebase emulators:start --only firestore --project "${FIRESTORE_PROJECT}" >/tmp/monkeybot-firestore-emulator.log 2>&1 &
  _FIRESTORE_EMULATOR_PID=$!
  wait_firestore_emulator_ready
}

ensure_firestore_emulator() {
  if [[ "${SKIP_FIRESTORE_EMULATOR:-}" == "1" ]]; then
    return 0
  fi
  if ! _playground_uses_firestore; then
    return 0
  fi

  export FIRESTORE_EMULATOR_HOST="127.0.0.1:${FIRESTORE_EMULATOR_HOST_PORT}"

  if _firestore_emulator_health_ok; then
    echo "run.sh: Firestore emulator already reachable at ${FIRESTORE_EMULATOR_HOST}"
    return 0
  fi

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if docker container inspect "${FIRESTORE_CONTAINER}" >/dev/null 2>&1; then
      published=$(docker port "${FIRESTORE_CONTAINER}" 8080/tcp 2>/dev/null || true)
      port_ok=0
      [[ "$published" == *":${FIRESTORE_EMULATOR_HOST_PORT}"* ]] && port_ok=1
      if [[ "$port_ok" -eq 0 ]]; then
        echo "run.sh: replacing Firestore emulator container (need -p ${FIRESTORE_EMULATOR_HOST_PORT}:8080)"
        docker rm -f "${FIRESTORE_CONTAINER}" >/dev/null
      elif [[ "$(docker container inspect -f '{{.State.Running}}' "${FIRESTORE_CONTAINER}" 2>/dev/null)" != "true" ]]; then
        echo "run.sh: starting Firestore emulator container ${FIRESTORE_CONTAINER}"
        docker start "${FIRESTORE_CONTAINER}" >/dev/null
      else
        echo "run.sh: Firestore emulator container already running (${FIRESTORE_CONTAINER})"
      fi
    fi

    if ! docker container inspect "${FIRESTORE_CONTAINER}" >/dev/null 2>&1; then
      echo "run.sh: starting Firestore emulator (${FIRESTORE_EMULATOR_IMAGE}, host port ${FIRESTORE_EMULATOR_HOST_PORT})"
      if ! docker run -d \
        --name "${FIRESTORE_CONTAINER}" \
        -p "${FIRESTORE_EMULATOR_HOST_PORT}:8080" \
        "${FIRESTORE_EMULATOR_IMAGE}" \
        sh -c 'gcloud beta emulators firestore start --host-port=0.0.0.0:8080' >/dev/null; then
        echo "run.sh: docker run failed for Firestore emulator (port ${FIRESTORE_EMULATOR_HOST_PORT} may be in use)."
      fi
    fi

    if wait_firestore_emulator_ready; then
      return 0
    fi
    echo "run.sh: Firestore emulator container not ready; trying gcloud/firebase fallback"
    docker rm -f "${FIRESTORE_CONTAINER}" >/dev/null 2>&1 || true
  fi

  if _start_firestore_emulator_gcloud; then
    return 0
  fi
  if _start_firestore_emulator_firebase; then
    return 0
  fi

  echo "run.sh: WARNING — could not start Firestore emulator."
  echo "run.sh: Install Docker, gcloud (firestore emulator), or firebase-tools, or set SKIP_FIRESTORE_EMULATOR=1 and point DB_URL at cloud Firestore."
  echo "run.sh: Logs (if any): /tmp/monkeybot-firestore-emulator.log"
  return 0
}

_firestore_emulator_cleanup() {
  if [[ "${PRESERVE_FIRESTORE_EMULATOR:-}" == "1" ]] || [[ "${SKIP_FIRESTORE_EMULATOR:-}" == "1" ]]; then
    return 0
  fi
  if [[ -n "${_FIRESTORE_EMULATOR_PID}" ]]; then
    if kill -0 "${_FIRESTORE_EMULATOR_PID}" 2>/dev/null; then
      echo "run.sh: stopping Firestore emulator process ${_FIRESTORE_EMULATOR_PID}"
      kill -TERM "${_FIRESTORE_EMULATOR_PID}" 2>/dev/null || true
      wait "${_FIRESTORE_EMULATOR_PID}" 2>/dev/null || true
    fi
    _FIRESTORE_EMULATOR_PID=""
    return 0
  fi
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  if ! docker container inspect "${FIRESTORE_CONTAINER}" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$(docker container inspect -f '{{.State.Running}}' "${FIRESTORE_CONTAINER}" 2>/dev/null)" == "true" ]]; then
    echo "run.sh: stopping Firestore emulator container ${FIRESTORE_CONTAINER}"
    docker stop -t 5 "${FIRESTORE_CONTAINER}" >/dev/null 2>&1 || true
  fi
}

# Stops OpenSandbox, Firestore emulator, and observability when the gateway exits (including Ctrl+C ending uv run).
trap '_run_sh_cleanup; _firestore_emulator_cleanup; _observability_cleanup' EXIT

ensure_opensandbox() {
  if [[ "${SKIP_OPENSANDBOX:-}" == "1" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "run.sh: docker not found; skipping OpenSandbox (set sandbox.enabled false or install Docker)."
    return 0
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "run.sh: Docker daemon not reachable; skipping OpenSandbox."
    return 0
  fi
  if [[ ! -f "${SANDBOX_CONFIG_HOST}" ]]; then
    echo "run.sh: missing ${SANDBOX_CONFIG_HOST}; skipping OpenSandbox."
    return 0
  fi

  if docker container inspect "${SANDBOX_CONTAINER}" >/dev/null 2>&1; then
    mounts=$(docker container inspect "${SANDBOX_CONTAINER}" --format '{{range .Mounts}}{{.Destination}};{{end}}')
    published=$(docker port "${SANDBOX_CONTAINER}" 8080/tcp 2>/dev/null || true)
    port_ok=0
    [[ "$published" == *":${SANDBOX_HOST_PORT}"* ]] && port_ok=1
    want_cfg_hash="$(_opensandbox_config_sha256)"
    got_cfg_hash=$(docker container inspect "${SANDBOX_CONTAINER}" --format '{{index .Config.Labels "mb.opensandbox.config_sha256"}}' 2>/dev/null || true)

    if [[ "${mounts}" != *"/etc/opensandbox/config.toml"* ]] || [[ "$port_ok" -eq 0 ]]; then
      echo "run.sh: replacing OpenSandbox container (need config mount + -p ${SANDBOX_HOST_PORT}:8080; got publish: ${published:-none})"
      docker rm -f "${SANDBOX_CONTAINER}" >/dev/null
    elif [[ -n "${want_cfg_hash}" && "${got_cfg_hash}" != "${want_cfg_hash}" ]]; then
      echo "run.sh: replacing OpenSandbox container (config file changed vs label mb.opensandbox.config_sha256)"
      docker rm -f "${SANDBOX_CONTAINER}" >/dev/null
    elif [[ "$(docker container inspect -f '{{.State.Running}}' "${SANDBOX_CONTAINER}")" != "true" ]]; then
      echo "run.sh: starting OpenSandbox container ${SANDBOX_CONTAINER}"
      docker start "${SANDBOX_CONTAINER}" >/dev/null
    else
      echo "run.sh: OpenSandbox container already running (${SANDBOX_CONTAINER})"
    fi
  fi

  if docker container inspect "${SANDBOX_CONTAINER}" >/dev/null 2>&1; then
    if wait_opensandbox_ready; then
      return 0
    fi
    echo "run.sh: OpenSandbox not ready after ${SANDBOX_HEALTH_WAIT_SECS}s; recreating container once"
    docker rm -f "${SANDBOX_CONTAINER}" >/dev/null
  fi

  echo "run.sh: starting OpenSandbox (${SANDBOX_IMAGE}, host port ${SANDBOX_HOST_PORT})"
  cfg_hash="$(_opensandbox_config_sha256)"
  if ! docker run -d \
    --name "${SANDBOX_CONTAINER}" \
    --label "mb.opensandbox.config_sha256=${cfg_hash}" \
    --add-host=host.docker.internal:host-gateway \
    -p "${SANDBOX_HOST_PORT}:8080" \
    -e OPENSANDBOX_INSECURE_SERVER=YES \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "${SANDBOX_CONFIG_HOST}:/etc/opensandbox/config.toml:ro" \
    "${SANDBOX_IMAGE}" >/dev/null; then
    echo "run.sh: docker run failed (port ${SANDBOX_HOST_PORT} may be in use). Fix the conflict or set SANDBOX_HOST_PORT."
    return 0
  fi
  wait_opensandbox_ready || true
}

ensure_opensandbox
ensure_observability_stack
ensure_firestore_emulator

# Wipe SQLite state on every launch so schema migrations / typed-block changes
# never leave the playground stuck on a stale DB (skipped when using Firestore).
if _playground_uses_sqlite; then
  rm -f \
    ./workspace/data/monkeybot.db ./workspace/data/monkeybot.db-wal ./workspace/data/monkeybot.db-shm \
    ./data/monkeybot.db ./data/monkeybot.db-wal ./data/monkeybot.db-shm
fi

echo "run.sh: starting MonkeyBot gateway…"
exit_code=0
if [[ -f .env ]]; then
  uv run --env-file .env -m monkeybot.gateway.main || exit_code=$?
else
  uv run -m monkeybot.gateway.main || exit_code=$?
fi
exit "${exit_code}"
