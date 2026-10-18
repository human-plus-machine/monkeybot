#!/usr/bin/env bash
# Local equivalent of .github/workflows/ci.yml.
# Push runs this with --if-needed and skips when this commit already passed.
# Live eval smoke is intentionally not included: it calls a hosted model.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if_needed=0
if [[ "${1:-}" == "--if-needed" ]]; then
  if_needed=1
fi

git_dir="$(git rev-parse --git-dir)"
stamp_file="$git_dir/ci-stamp"
head_sha="$(git rev-parse HEAD)"

if [[ "$if_needed" -eq 1 && -z "$(git status --porcelain)" && -f "$stamp_file" && "$(<"$stamp_file")" == "$head_sha" ]]; then
  echo "Local CI already passed for $head_sha"
  exit 0
fi

missing=()
command -v rg >/dev/null 2>&1 || missing+=(ripgrep)
command -v gh >/dev/null 2>&1 || missing+=(gh)
if ((${#missing[@]} > 0)); then
  echo "Local CI needs: ${missing[*]}. Install with: brew install ${missing[*]}" >&2
  exit 1
fi

export CI=true

uv sync --extra claude --extra vertex-claude --extra bedrock --extra firestore --extra evals
uv run mypy src/monkeybot
uv run pytest tests/ -q

(
  cd cli
  uv sync
  uv run pytest -q
)

(
  cd integrations/browser-mcp
  uv sync --dev
  uv run pytest -q
)

uv run python evals/test_assertions.py
uv run python evals/test_report.py
uv run python evals/test_runner_sse.py

if [[ -z "$(git status --porcelain)" ]]; then
  printf '%s\n' "$head_sha" >"$stamp_file"
fi
