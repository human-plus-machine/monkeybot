#!/usr/bin/env bash
# Clean-machine smoke for global CLI distribution (local wheel stand-in for PyPI).
#
# Builds core + CLI wheels, installs monkeybot-cli into an isolated UV_TOOL_* tree
# (no editable clone), scaffolds an agent, uv syncs from the same wheel dir, then
# runs validate / doctor / one fake chat turn.
#
# Usage (from repo root):
#   bash scripts/smoke_global_cli.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELS="$(mktemp -d "${TMPDIR:-/tmp}/mb-wheels.XXXXXX")"
SMOKE="$(mktemp -d "${TMPDIR:-/tmp}/mb-smoke.XXXXXX")"
TOOL="$(mktemp -d "${TMPDIR:-/tmp}/mb-tool.XXXXXX")"
cleanup() {
  rm -rf "$WHEELS" "$SMOKE" "$TOOL"
}
trap cleanup EXIT

echo "==> Building wheels into $WHEELS"
uv build --out-dir "$WHEELS" "$ROOT"
uv build --out-dir "$WHEELS" "$ROOT/cli"
ls -1 "$WHEELS"

export UV_TOOL_DIR="$TOOL/tools"
export UV_TOOL_BIN_DIR="$TOOL/bin"
export PATH="$UV_TOOL_BIN_DIR:$PATH"
# Prefer local wheels over PyPI for monkeybot / monkeybot-cli.
export UV_FIND_LINKS="$WHEELS"
export UV_INDEX_STRATEGY="unsafe-best-match"
# Don't inherit the harness checkout venv into the smoke agent project.
unset VIRTUAL_ENV

echo "==> uv tool install monkeybot-cli (isolated tool env)"
uv tool uninstall monkeybot-cli >/dev/null 2>&1 || true
uv tool install --find-links "$WHEELS" monkeybot-cli

command -v monkeybot >/dev/null
monkeybot --help >/dev/null

BOT="$SMOKE/bot"
echo "==> monkeybot new --provider fake"
monkeybot new --dest "$BOT" --provider fake --model fake-model --yes

test -f "$BOT/pyproject.toml"
test -f "$BOT/monkeybot_config/monkeybot.yaml"
grep -q 'monkeybot>=2.1.0,<3\|monkeybot\[.*\]>=2.1.0,<3' "$BOT/pyproject.toml"
# fake has no provider extra — bare monkeybot range
grep -q '"monkeybot>=2.1.0,<3"' "$BOT/pyproject.toml"
grep -q 'provider: fake' "$BOT/monkeybot_config/monkeybot.yaml"

echo "==> uv sync (agent project, find-links=$WHEELS)"
(
  cd "$BOT"
  uv sync --find-links "$WHEELS"
)

echo "==> monkeybot validate"
monkeybot validate --cwd "$BOT" --json | tee "$SMOKE/validate.json"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d.get('ok') is True, d" "$SMOKE/validate.json"

echo "==> monkeybot doctor"
monkeybot doctor --cwd "$BOT" --json | tee "$SMOKE/doctor.json"
python3 - <<'PY' "$SMOKE/doctor.json"
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("ok") is True, d
# Missing-extra remediation must not push uv sync --extra for MVP layout.
blob = json.dumps(d)
assert "uv sync --extra" not in blob, blob
# Fake provider must not demand credentials.
creds = next(c for c in d["checks"] if c["id"] == "provider.credentials.present")
assert creds["status"] == "pass", creds
print("doctor ok")
PY

echo "==> monkeybot chat (one fake turn)"
# Pick a free port so we don't collide with a local gateway.
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export MONKEYBOT_CHAT_PLAIN=1
export MONKEYBOT_CHAT_NO_ANIMATIONS=1
# Ensure yaml/runtime use our port — doctor already checked env; override via env.
export PORT
printf 'hello\n/bye\n' | monkeybot chat --cwd "$BOT" --port "$PORT" || {
  echo "chat failed" >&2
  exit 1
}

echo
echo "OK: global CLI smoke passed (tool install → new → sync → validate → doctor → chat)"
