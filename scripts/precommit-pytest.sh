#!/usr/bin/env bash
# Pre-commit hook: run changed test files, or a fast collection check for src-only edits.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  exit 0
fi

test_files=()
for path in "$@"; do
  [[ -f "$path" ]] || continue
  # Fixture repos ship their own tests. They are not part of this suite, and
  # several are meant to fail inside the copied workspace.
  [[ "$path" == regression/fixtures/workspaces/* ]] && continue
  # Phase 5 is opt-in. A commit or pull request does not run it.
  [[ "$path" == regression/t5/* ]] && continue
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
  uv run pytest tests regression --co -q
fi
