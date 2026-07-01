# AGENTS.md

## Cursor Cloud specific instructions

MonkeyBot is a Python 3.11+ framework (managed with `uv`) whose product is the **FastAPI SSE gateway** (`python -m monkeybot.gateway.main`). Satellite sub-projects each have their own `pyproject.toml`/`uv.lock`/`.venv` and depend on the core via an editable path: `cli/` (the `monkeybot` CLI, recommended entrypoint), `integrations/browser-mcp/` (MCP server), and `evals/` (standalone eval service). Standard install/lint/test/build commands are documented in `README.md`, `docs/getting-started.md`, and `.github/workflows/ci.yml` (the authoritative CI recipe) — refer to those rather than duplicating them.

The startup update script already runs `uv sync` for the root (with the CI extras plus `evals` and `web-search`) and for `cli/` and `integrations/browser-mcp/`, so dependencies are ready. `uv` installs to `~/.local/bin`; if `uv` is not found, add that to `PATH`.

Non-obvious gotchas discovered during setup:

- **Run the gateway with no cloud credentials via `MODEL_PROVIDER=fake`.** It injects a deterministic scripted provider (default reply `hello`), exercising the full session → SSE → agent-loop → SQLite path without any API keys. Real providers (`gemini`, `openai`, `anthropic`, …) need keys/ADC in a repo-root `.env`.
- **Running the gateway requires scaffolded config** at the process cwd: `cd cli && uv run monkeybot new --dest .. --yes` creates `monkeybot_config/`, `workspace/`, and `data/` at the repo root (all gitignored except the stray `.env.example`/`scripts/setup-workspace.sh` — do not commit those). Do NOT put scaffolding in the update script.
- **The root pytest suite must NOT be run with the scaffold present.** A scaffolded `monkeybot_config/` + `workspace/` at the repo root makes 2 workspace-resolution tests fail (`tests/gateway/sse/test_routes.py::test_workspace_tree_and_file` and `tests/skills/test_generate_image_script.py::test_generate_image_script_success_mocked_vertex`); both pass in isolation. CI passes because it runs from a fresh checkout with no scaffold. Before `uv run pytest tests/ -q`, run tests in a clean tree (move `monkeybot_config/`, `workspace/`, `data/` aside, or use a checkout that was never scaffolded).
- **`ruff` is not enforced by CI** and currently reports pre-existing failures across `src/` and `tests/`. CI gates only `mypy src/monkeybot` + pytest (root, `cli/`, `integrations/browser-mcp/`) + the three `evals/test_*.py` standalone self-check scripts. Do not treat existing `ruff check .` failures as regressions.
- **`monkeybot doctor` reporting port 8080 "in use" or "no provider credentials" is expected** when the gateway is already running and/or you are using the `fake` provider — it is not a setup failure.

## Git workflow

Always sync with the remote **before** writing code — do not assume the local checkout is current.

**Starting a new feature:**

1. `git fetch origin develop && git checkout develop && git pull origin develop`
2. Create the feature branch from that fresh tip: `git checkout -b cursor/<descriptive-name>-<suffix>`

**Continuing an existing feature branch:**

1. `git fetch origin <branch-name> && git checkout <branch-name> && git pull origin <branch-name>`

**When the base branch (`develop`) has moved** and you need those changes on your feature branch:

- `git fetch origin develop` then `git merge origin/develop` (or `git rebase origin/develop` if that is the project convention)

Check `git status` and `git branch -a` first when the environment starts on a detached `HEAD` or an unfamiliar checkout.
