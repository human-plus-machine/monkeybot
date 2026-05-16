#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Wipe SQLite state on every launch so schema migrations / typed-block changes
# never leave the playground stuck on a stale DB.
rm -f ./data/monkeybot.db ./data/monkeybot.db-wal ./data/monkeybot.db-shm

if [[ -f .env ]]; then
  exec uv run --env-file .env -m monkeybot.gateway.main
fi
exec uv run -m monkeybot.gateway.main
