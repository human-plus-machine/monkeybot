# Contributing

Thanks for helping improve monkeybot. Report bugs and doc gaps via [issues](https://github.com/human-plus-machine/monkeybot/issues); PRs for fixes and improvements are welcome.

## Developing the harness

Clone only if you are changing monkeybot itself (end users should use `uv tool install monkeybot-cli` — see the [README](README.md)):

```bash
git clone https://github.com/human-plus-machine/monkeybot.git
cd monkeybot && uv sync
cd cli && uv sync
uv tool install --editable .
```

For live eval smoke runs against a local gateway, use [`evals/smoke_agent/`](evals/smoke_agent/) (see [docs/live-evals.md](docs/live-evals.md)).

### Checks

CI runs `mypy` on `src/monkeybot` and pytest (root, `cli/`, `integrations/browser-mcp/`), plus standalone eval self-checks. Before submitting:

```bash
uv run pytest
uv run mypy src/monkeybot
```

`ruff` is useful locally but is not a CI gate today.

> [!NOTE]
> Do not run the root pytest suite with a scaffolded `monkeybot_config/` + `workspace/` at the repo root — that layout can make a couple of workspace-resolution tests fail. CI uses a clean tree.

## Releasing

`develop` is the working branch; `main` always reflects the latest release.

1. Anyone runs the **Prepare release** workflow (Actions tab → Prepare release → Run workflow), choosing `core` or `cli` and a version bump. It bumps the package's `pyproject.toml`, moves the `CHANGELOG.md` `Unreleased` section into a dated entry on a `release/<package>-v<version>` branch, and opens a PR from that branch into `main` (`develop` itself is untouched until the PR merges, so an abandoned release PR leaves no trace).
2. An admin reviews and **merges the PR** (branch protection on `main` restricts who can merge). Use a regular merge commit, not squash, so `main`'s history and the release tag line up with what was reviewed.
3. The **Publish release** workflow runs automatically on that merge: it tags the new version, creates a GitHub Release from the changelog entry, Trusted-Publishes the released package(s) to PyPI (core before CLI when both ship), and merges `main` back into `develop` so the two branches don't drift apart.

One-time setup (repo Settings → Branches → add rule for `main`): require a pull request before merging, and restrict who can push to matching branches to Admins. That's the only access control needed — anyone can prepare a release, only admins can promote it to `main`.

**Before relying on this:** `develop` and `main` currently have diverged history (independent commits on each side). Reconcile them once with a manual merge before running the first automated release, or the first release PR may show unrelated changes or conflicts. `CHANGELOG.md` is shared across both packages — `core` and `cli` versions are bumped independently but their release notes live in the same file. The current versions already on `main` (`core` 2.0.0, `cli` 0.1.7) predate this tooling and have no changelog entry, so the first `publish` run intentionally skips tagging them rather than creating a release with empty notes; tagging starts from the next real version bump.
