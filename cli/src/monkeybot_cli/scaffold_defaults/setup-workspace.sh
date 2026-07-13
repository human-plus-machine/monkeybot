#!/usr/bin/env bash
# Ensure workspace/skills and project-root memory/ exist (no skills symlink).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="$ROOT/workspace"
SKILLS="$WS/skills"
MEMORY="$ROOT/memory"

mkdir -p "$SKILLS" "$MEMORY"
if [[ ! -f "$MEMORY/INDEX.md" ]]; then
  printf '%s\n' "# Memory index" "" "Add sections here or let memory tools populate this file." > "$MEMORY/INDEX.md"
fi

echo "Ready: workspace/skills and memory/"
