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

# Hooks export GIT_DIR and other repo-local vars. Left set, `git init` and
# `git -C` in tests operate on this checkout instead of the temp workspace they
# create. Git lists those vars itself; SSH and auth vars stay set.
unset $(git rev-parse --local-env-vars)

# uv sync refreshes the editable package version in lockfiles. pre-commit
# rejects the push if this hook leaves those files changed, so restore them.
lock_backup="$(mktemp -d)"
cp uv.lock "$lock_backup/root.lock"
cp cli/uv.lock "$lock_backup/cli.lock"
cp integrations/browser-mcp/uv.lock "$lock_backup/browser.lock"
restore_locks() {
  cp "$lock_backup/root.lock" uv.lock
  cp "$lock_backup/cli.lock" cli/uv.lock
  cp "$lock_backup/browser.lock" integrations/browser-mcp/uv.lock
  rm -rf "$lock_backup"
}
trap restore_locks EXIT

uv sync --extra claude --extra vertex-claude --extra bedrock --extra firestore --extra evals
uv run ruff format --check .
uv run mypy src/monkeybot
# Each worker is its own process. Tests keep state in tmp_path, so the
# suite is safe to fan out across the machine.
uv run pytest tests/ -q -n auto

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
uv run python evals/test_coding_hard_oracle.py

if [[ -z "$(git status --porcelain)" ]]; then
  printf '%s\n' "$head_sha" >"$stamp_file"
fi
