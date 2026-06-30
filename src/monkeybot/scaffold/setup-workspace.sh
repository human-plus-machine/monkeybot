#!/usr/bin/env bash
# Ensure workspace/ exists and workspace/skills points at skills/ (for read_file in sandbox).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="$ROOT/workspace"
SKILLS_SRC="$ROOT/skills"
LINK="$WS/skills"

mkdir -p "$WS"
touch "$WS/.gitkeep"
mkdir -p "$SKILLS_SRC"

if [[ -L "$LINK" ]]; then
  echo "workspace/skills symlink already exists"
  exit 0
fi

if [[ -e "$LINK" ]]; then
  echo "error: $LINK exists and is not a symlink — remove it or run from a clean scaffold" >&2
  exit 1
fi

ln -sfn "../skills" "$LINK"
echo "Created workspace/skills -> ../skills"
