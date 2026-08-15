#!/usr/bin/env bash
# Pre-commit hook: run changed test files, or a fast collection check for src-only edits.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  exit 0
fi

test_files=()
for path in "$@"; do
  [[ -f "$path" ]] || continue
  base="${path##*/}"
  case "$base" in
    test_*.py|*_test.py|conftest.py)
      test_files+=("$path")
      ;;
  esac
done

if ((${#test_files[@]} > 0)); then
  uv run pytest "${test_files[@]}" -q --tb=line --no-header && exit 0
  code=$?
  # Exit 5: no tests collected (helper modules, empty batches).
  [[ $code -eq 5 ]] && exit 0
  exit "$code"
else
  uv run pytest tests/ -q --co -q --no-header
fi
