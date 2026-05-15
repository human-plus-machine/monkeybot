#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  echo "Copy .env.example to .env in playground/agent/, then edit if needed." >&2
  exit 1
fi

# Wipe SQLite state on every launch so schema migrations / typed-block changes
# never leave the playground stuck on a stale DB.
rm -f ./data/monkeybot.db ./data/monkeybot.db-wal ./data/monkeybot.db-shm

exec uv run --env-file .env -m monkeybot.gateway.main
