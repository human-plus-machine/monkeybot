# Changelog

All notable changes to monkey-bot (`emonk`) are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

<!-- BEGIN harness-extensibility story 9 -->
## [Unreleased]

### Changed
- **Documentation:** renamed the "Universal Agent Harness" narrative to **Agent Harness**;
  primary guide is now [`docs/agent-harness.md`](docs/agent-harness.md) (replaces
  `docs/universal-agent-harness.md`). Added
  [`docs/creating-a-harness-agent.md`](docs/creating-a-harness-agent.md) as the
  end-to-end harness walkthrough. Removed internal planning trees
  (`docs/preplanning/`, `docs/phases/`) from the published tree; `.monkeymode/`
  is gitignored for local-only workflow state.
- **Packaging:** `pyproject.toml` authors set to `human+machine`; repository URLs
  point at `https://github.com/human-and-machine/monkey-bot`.

### Added
- **Open-source hygiene:** root `LICENSE` (MIT), `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, and GitHub issue templates under `.github/ISSUE_TEMPLATE/`.
- **Harness extensibility layer.** Six pluggable extension surfaces
  (`Checkpointer`, `MemoryStore`, `JobStorage`, `IdentitySource`,
  `SecretResolver`, `ModelProvider`), 32 shipped reference backends, and a
  three-mechanism registration story (programmatic, `import_path` in YAML,
  opt-in pip entry points). See
  [`docs/extending-the-harness.md`](docs/extending-the-harness.md).
- **Public contract-test suite.** `emonk.core.harness.extensions.testing`
  exposes `checkpointer_contract_suite`, `memory_store_contract_suite`,
  `job_storage_contract_suite`, `identity_source_contract_suite`,
  `secret_resolver_contract_suite`, and `model_provider_contract_suite` —
  the exact invariants shipped backends pass, callable from consumer repos.
- **Canonical DynamoDB Checkpointer example** at
  [`examples/extension-dynamodb-checkpointer/`](examples/extension-dynamodb-checkpointer/)
  — a ~100-LOC pip-installable plugin that declares an
  `emonk.checkpointers` entry point and drives the framework contract suite
  under `moto`.
- **`Dockerfile.extension-template`** at the repo root — the supply-chain-safe
  image template for consumers bundling harness extensions. Ships
  `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1` and
  `pip install --require-hashes` out of the box.
- **Extension documentation suite.** Eight guides under `docs/harness/`
  (backend matrix, identity sources, secret resolvers, model providers,
  AWS enterprise runbook, Postgres backends, Mongo backends, plugin
  operations), each ending with a runnable, framework-modification-free
  snippet.

### Removed
- `emonk.core.harness.checkpointer.DynamoDBCheckpointerStub` — the legacy
  `NotImplementedError` placeholder. AWS enterprises default to the new
  `PostgresCheckpointer`; deployments that want native DynamoDB durability
  copy the canonical worked example at
  [`examples/extension-dynamodb-checkpointer/`](examples/extension-dynamodb-checkpointer/).
<!-- END harness-extensibility story 9 -->

<!-- BEGIN harness-extensibility phase 6 -->
### Changed (Phase 6 — Integration)
- **`JobStorage` builtin registered under canonical name `json`** (matches
  `JobStorageJSONSpec.backend` literal); the old name `json_file` stays
  registered as a back-compat alias.
- **`/harness/aws/smoke` response reshaped to the 1B §7.4 contract**: the
  endpoint now returns `{"checks": [...], "all_pass": bool}` and emits
  `HTTP 503 HARNESS_AWS_SMOKE_FAIL` when any probe fails. Legacy
  `{"ok", "probes"}` fields stay in the body for back-compat with existing
  consumers.
- **Identity events now flow through the shared `EventBus`.** The assembler
  plumbs both `event_bus` and `VersionTriple` into `IdentityResolutionMW`,
  and the middleware publishes real `HarnessEvent`s for
  `identity.load` / `identity.load_failed` / `identity.cache_evict` /
  `identity.bust` (previously logged only). Handlers can now observe
  identity flow end-to-end.
- **`IdentityResolutionMW` enforces R-16 single-flight.** Concurrent
  cold-miss lookups for the same `(principal_id, session_id)` collapse to
  a single backend `load()` call, protecting the identity source from
  stampede on warm-up or cache expiry.
- **`IdentityCache` supports `on_evict` callbacks.** TTL, capacity, and
  explicit `invalidate()` evictions all notify subscribers, enabling the
  new `IDENTITY_BUST` / `IDENTITY_CACHE_EVICT` event emission without the
  cache depending on the event module.
- **`CompiledAgent` exposes direct handles** to the resolved ABC-based
  extension instances: `memory_store`, `job_storage`, `checkpointer_ext`,
  plus a `checkpointer` property that routes to the ABC instance when
  present and falls back to the legacy `SessionRegistry.checkpointer`.
- **OpenAI / Anthropic API keys are wrapped in `pydantic.SecretStr`** at
  the model-provider boundary so they never leak via model `repr()` or
  structured logging.
- **Legacy `emonk.core.harness.checkpointer.FirestoreCheckpointer` is
  deprecated.** New deployments should use the ABC-based
  `src.core.harness.extensions.checkpointers.firestore.FirestoreCheckpointer`
  (discriminated via `checkpointer: { backend: firestore }`). The legacy
  class stays functional with a `DeprecationWarning` so zero-change bots
  keep working.
- **Assembler does NOT auto-synthesize a `LocalFSIdentitySource`** from
  `cfg.identity` — opting into the identity-source middleware is an
  explicit `cfg.identity_source` stanza. This preserves the zero-change
  regression gate for marketing-bot / coding-bot pipelines.

### Added (Phase 6 — Integration)
- **Cross-story integration tests** under `tests/integration/`:
  - `test_full_stack_wiring.py` — builds a HarnessConfig exercising every
    extension surface and asserts end-to-end wiring into `CompiledAgent`.
  - `test_identity_events_emitted.py` — asserts `IDENTITY_LOAD` /
    `IDENTITY_BUST` reach the bus, and the no-bus fallback logs safely.
  - `test_cache_single_flight.py` — asserts R-16 collapses 20 concurrent
    cold misses into exactly one backend `load()`.
<!-- END harness-extensibility phase 6 -->
