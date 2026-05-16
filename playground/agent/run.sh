#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# OpenSandbox API (matches monkeybot_config/monkeybot.yaml sandbox.server_url default).
# Set SKIP_OPENSANDBOX=1 to skip. Requires Docker; sandboxes need the host socket.
SANDBOX_CONTAINER="${SANDBOX_CONTAINER:-monkeybot-playground-opensandbox}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-opensandbox/server:latest}"
SANDBOX_HOST_PORT="${SANDBOX_HOST_PORT:-18080}"
# Docker runtime + bindable API (see monkeybot_config/opensandbox.docker.toml).
SANDBOX_CONFIG_HOST="${SANDBOX_CONFIG_HOST:-$(pwd)/monkeybot_config/opensandbox.docker.toml}"
# Seconds to wait for OpenSandbox /health after docker start/run (server pulls images on first boot).
SANDBOX_HEALTH_WAIT_SECS="${SANDBOX_HEALTH_WAIT_SECS:-5}"

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

# Stops OpenSandbox when the gateway exits (including Ctrl+C ending uv run).
trap '_run_sh_cleanup' EXIT

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

# Wipe SQLite state on every launch so schema migrations / typed-block changes
# never leave the playground stuck on a stale DB.
rm -f ./data/monkeybot.db ./data/monkeybot.db-wal ./data/monkeybot.db-shm

exit_code=0
if [[ -f .env ]]; then
  uv run --env-file .env -m monkeybot.gateway.main || exit_code=$?
else
  uv run -m monkeybot.gateway.main || exit_code=$?
fi
exit "${exit_code}"
