#!/usr/bin/env bash
# Pre-commit hook: run changed test files, or a fast collection check for src-only edits.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  exit 0
fi

test_files=()
for path in "$@"; do
  case "$path" in
    tests/*.py|tests/**/*.py)
      test_files+=("$path")
      ;;
  esac
done

if ((${#test_files[@]} > 0)); then
  uv run pytest "${test_files[@]}" -q --tb=line --no-header
else
  uv run pytest tests/ -q --co -q --no-header
fi
