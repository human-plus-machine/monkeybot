#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  echo "Copy .env.example to .env in playground/agent/, then edit if needed." >&2
  exit 1
fi
exec uv run --env-file .env -m monkeybot.gateway.main
