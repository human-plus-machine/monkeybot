# Global CLI distribution (PyPI)

Goal: new users install the `monkeybot` CLI globally **without cloning the repo**, then scaffold and run an agent anywhere on their machine.

```bash
# one-time toolchain
curl -LsSf https://astral.sh/uv/install.sh | sh

# install CLI globally (puts `monkeybot` on PATH)
uv tool install monkeybot-cli

# create an agent anywhere
mkdir ~/agents/my-bot && cd ~/agents/my-bot
monkeybot new --yes
uv sync                          # installs agent pyproject.toml deps (core + provider)
# copy .env.example → .env, add provider keys, edit monkeybot_config/AGENT.md
monkeybot doctor
monkeybot chat
```

Cloning the monkeybot repo is for **contributors / harness development only**, not day-1 users.

Out of scope for this plan: interim `git+https://…#subdirectory=cli` installs, and an `npx` npm shim (optional follow-up later).

---

## Why this was blocked (historical)

1. **Was not on PyPI** — addressed by the publish pipeline + first release (step 7).
2. **CLI depends on a local path for in-repo development** — `cli/pyproject.toml` has:

   ```toml
   [tool.uv.sources]
   monkeybot = { path = "..", editable = true }
   ```

   That path source is **correct and should stay** for clone-based development. It is **not** emitted in published wheel metadata, so the PyPI CLI package resolves `monkeybot` from the index once the project dependency is a version range (see below). No separate workspace/override packaging scheme is required.
3. **Release CI tags GitHub Releases only** — `.github/workflows/publish-release.yml` + `scripts/release.py publish` create tags/`gh release`; they do **not** upload wheels to PyPI.

The product model is already correct for a global CLI:

- CLI is thin (`monkeybot-cli`).
- **Optional** provider/storage extras are declared on the **agent project** (`monkeybot[openai]`, `monkeybot[postgres]`, …) and installed with plain `uv sync`.
- `google-genai` is presently part of **core** (`monkeybot` base dependencies), not an agent-owned optional. Agent-owned deps apply to *optional* extras (providers beyond what core already pulls, storage backends, sandbox, observability, etc.).
- `monkeybot new` copies config from packaged CLI defaults (`monkeybot_cli.scaffold_defaults`).
- `monkeybot run` / `chat` prefer the agent’s `.venv` / `uv run`, then fall back to the CLI interpreter.

---

## Target user journey

| Step | User action | Result |
|------|-------------|--------|
| 1 | Install `uv` | Python toolchain available |
| 2 | `uv tool install monkeybot-cli` | `monkeybot` on PATH (isolated tool env) |
| 3 | `monkeybot new --dest ~/agents/foo --provider …` | Scaffolded agent + **`pyproject.toml`** (MVP) |
| 4 | `uv sync` in the agent dir | Core + selected provider extra installed into agent `.venv` |
| 5 | Edit `.env` + `AGENT.md` | Credentials + system prompt |
| 6 | `monkeybot validate` / `doctor` / `chat` | Running agent |

Upgrade path:

```bash
uv tool upgrade monkeybot-cli
```

---

## Work items

### 1. Package layout for PyPI

**Publish two packages:**

| Package | Source | Console script | Role |
|---------|--------|----------------|------|
| `monkeybot` | repo root `pyproject.toml` | none (library + gateway modules) | Harness runtime |
| `monkeybot-cli` | `cli/pyproject.toml` | `monkeybot` → `monkeybot_cli.main:main` | User-facing CLI |

**Published CLI dependency (project metadata):**

```toml
dependencies = [
  "monkeybot[cli]>=2.1.0,<3",
  # … other CLI deps
]
```

**In-repo development (unchanged):**

```toml
[tool.uv.sources]
monkeybot = { path = "..", editable = true }
```

Keep this path source for local clones. `[tool.uv.sources]` is **not** part of wheel metadata, so published `monkeybot-cli` installs resolve `monkeybot[cli]>=2.1.0,<3` from PyPI with no workspace/override scheme.

### 2. Scaffold: agent `pyproject.toml` (MVP requirement)

`monkeybot new` **must** generate an agent-project `pyproject.toml` as part of MVP.

Requirements:

- Depends on a **compatible core version range** (aligned with the CLI’s supported `monkeybot` bound, e.g. `monkeybot[…]>=2.1.0,<3`).
- Includes the **selected provider extra** from `--provider` (mapped to the correct optional-extra name, e.g. `anthropic` → `claude`, `aws_bedrock` → `bedrock`).
- After scaffold, the user runs **plain `uv sync`** in the agent directory (not `uv sync --extra …`).
- Do **not** point the agent at a local harness path; published agents depend on PyPI `monkeybot`.

Example shape (illustrative):

```toml
[project]
name = "my-bot"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "monkeybot[openai]>=2.1.0,<3",
]
```

Clarify dependency ownership for docs/skill:

| Dependency class | Who owns it | Notes |
|------------------|-------------|--------|
| Core harness | Agent `pyproject.toml` via `monkeybot[…]` | Includes base runtime; **`google-genai` is presently in core** |
| Optional provider extras | Agent `pyproject.toml` | e.g. `openai`, `claude`, `bedrock`, `ollama`, … |
| Optional storage / features | Agent `pyproject.toml` | e.g. `postgres`, `firestore`, `sandbox`, `observability` |
| Global CLI tool env | `uv tool install monkeybot-cli` | Thin; does not replace agent extras |

Config-only trees (no `pyproject.toml`) may remain as advanced/legacy; MVP path always scaffolds `pyproject.toml`.

### 3. Doctor remediation copy

Change provider/extra remediation from:

```text
cd <agent> && uv sync --extra <provider>
```

to (default MVP wording):

```text
Add monkeybot[<provider>] to the agent dependencies, then run uv sync
```

(or equivalent that names the agent `pyproject.toml` dependency edit explicitly).

**Exception:** if the scaffold deliberately defines **project-level extras** on the agent `pyproject.toml` (so `uv sync --extra …` is the intended workflow), doctor may keep `--extra` remediation for that layout. Default scaffold should **not** require project-level extras — plain `uv sync` after editing/adding `monkeybot[<extra>]` in `dependencies`.

Update skill / getting-started provider tables to match (install column = add dep + `uv sync`, not `uv sync --extra`).

### 4. Versioning and release coupling

Today:

- Core: `monkeybot` version in root `pyproject.toml` (e.g. `2.1.0`)
- CLI: `monkeybot-cli` version in `cli/pyproject.toml` (e.g. `0.2.0`)
- Tags: `core-v*` and `cli-v*` via `scripts/release.py`

Policy:

- Keep independent versions; every `monkeybot-cli` release declares `monkeybot[cli]>=X,<3` (floor raised when needed).
- When scaffolding or gateway APIs change in a breaking way, bump core major/minor and raise the CLI’s dependency floor in the same release train.
- Changelog: continue using `CHANGELOG.md`; ensure both packages get clear Unreleased → versioned entries when publishing.
- Scaffolded agent `pyproject.toml` should use the same compatible core range the CLI advertises.

### 5. PyPI publish pipeline (extend `publish-release.yml` only)

**Do not** add a second tag-triggered workflow.

Extend the existing [`.github/workflows/publish-release.yml`](../.github/workflows/publish-release.yml) so that the same run which cuts GitHub release tags also:

1. Builds wheels/sdists for packages **released by that run** only (the ones `scripts/release.py publish` just tagged — not every historical tag).
2. Publishes those packages to PyPI via **Trusted Publishing** (OIDC), not long-lived API tokens.
3. Publishes core before CLI when both are in the same run (CLI depends on core).

Checklist for first production publish:

- [ ] Create GitHub Environments `pypi` (core) and `pypi-cli` (CLI) on `human-plus-machine/monkeybot`.
- [ ] Register pending Trusted Publishers (different environments — PyPI allows only one pending publisher per repo/workflow/environment):
  - `monkeybot` → workflow `publish-release.yml`, environment `pypi`
  - `monkeybot-cli` → workflow `publish-release.yml`, environment `pypi-cli`
- [ ] Dry-run against TestPyPI using the same workflow shape (optional).
- [ ] Smoke-test on a clean VM/container: `uv tool install monkeybot-cli` → `monkeybot new` → `uv sync` → `monkeybot doctor`.

### 6. Packaged scaffold resources (verification only)

Scaffold templates already live in **`monkeybot_cli.scaffold_defaults`** (moved out of the harness). Treat wheel inclusion as a **verification check**, not implementation work:

- Confirm the published **CLI** wheel exposes `importlib.resources` for `monkeybot_cli.scaffold_defaults` (yaml/json/md/sh).
- Confirm `monkeybot new` works from a clean `uv tool install monkeybot-cli` with no clone.
- No new packaging design needed beyond verifying hatch/wheel contents on the release candidate.

Repo-root `monkeybot_config_example/` remains human-readable full-option examples; it is not the import path for `monkeybot new`.

### 7. Docs and onboarding copy

Update these to lead with global install (clone becomes “Contributing” / “Developing the harness”):

- [x] `README.md` — Installation section
- [x] `docs/getting-started.md`
- [x] `cli/skills/monkeybot/SKILL.md` (Tier 0 — remove “not on PyPI / clone first” as the default path; fix provider install / doctor remediation language)
- [x] Any deploy / CLI docs that say `cd cli && uv tool install --editable .` as the primary path

New primary install snippet:

```bash
uv tool install monkeybot-cli
monkeybot new --dest ./my-agent --provider openai --yes
cd my-agent
uv sync
cp .env.example .env
monkeybot doctor
monkeybot chat
```

Contributor path (secondary — keeps path source):

```bash
git clone https://github.com/human-plus-machine/monkeybot.git
cd monkeybot && uv sync
cd cli && uv sync
uv tool install --editable .
```

---

## Acceptance criteria

A person with only `uv` installed (no monkeybot git clone) can:

1. `uv tool install monkeybot-cli`
2. `monkeybot new --dest /tmp/mb-smoke --provider fake --yes` (or equivalent) creates `monkeybot_config/` **and** `pyproject.toml` depending on `monkeybot[<extra>]` in the compatible core range
3. `cd /tmp/mb-smoke && uv sync` succeeds
4. `monkeybot validate --cwd /tmp/mb-smoke` → ok
5. `monkeybot doctor --cwd /tmp/mb-smoke` remediation (when an extra is missing) says to add `monkeybot[<extra>]` then `uv sync`, not `uv sync --extra …` (unless project-level extras were deliberately scaffolded)
6. `monkeybot chat --cwd /tmp/mb-smoke` completes one turn (fake or real provider)

Contributors can still develop against a local clone with the existing `[tool.uv.sources]` path editable without publishing.

Release: a push to `main` that runs `publish-release.yml` tags **and** Trusted-Publishes only the packages released in that run.

---

## Suggested implementation order

1. [x] Set published CLI dep to `monkeybot[cli]>=2.1.0,<3`; keep `[tool.uv.sources]` for in-repo dev.
2. [x] MVP: `monkeybot new` writes agent `pyproject.toml` (core range + provider extra); document plain `uv sync`.
3. [x] Update `doctor` remediation (+ skill/docs tables).
4. [x] Extend `publish-release.yml` with Trusted Publishing for packages tagged in that run (no second workflow).
5. [x] Verification: CLI wheel contains `scaffold_defaults`; clean-machine smoke (`tool install` → `new` → `uv sync` → `doctor` / `chat`).
6. [x] Flip README / skill / getting-started to the global-install path.
7. First real PyPI release (core then CLI when both ship).

---

## Non-goals

- Requiring users to clone this repo to create an agent.
- Shipping every provider/storage extra inside the global CLI by default.
- Replacing agent-owned `.venv` resolution with a single global runtime for all agents.
- Interim git+subdirectory CLI installs.
- A second tag-triggered publish workflow.
- Treating `google-genai` as an agent-owned optional while it remains in core (document current reality; revisit only if core deps change).
