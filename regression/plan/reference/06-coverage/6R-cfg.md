# 6.R Config, bootstrap, reload (`CFG`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`.

Existing: T1 `test_runtime_env.py`, `test_runtime_config_snapshot.py`, `test_config_extensions.py`, `test_bootstrap.py`, gateway `test_config_reload.py`.

| ID | Case | Asserts |
|---|---|---|
| T1-CFG-01 | `precedence_matrix` | Table: env vs YAML vs `includes` vs `MONKEYBOT_CONFIG`; `model.*` YAML-only; leftover `MODEL_PROVIDER` without YAML raises. |
| T1-CFG-02 | `every_env_key_has_tier` | Every `ENV_MAP` key maps to exactly one `ConfigTier`; a new key without a tier fails. |
| T1-CFG-03 | `retired_sections_warned` | `knowledge:`, `context_curation:` warned and ignored. |
| T1-CFG-04 | `yaml_validation_rejects` | `memory.backend: s3`, unknown provider ids, `MODEL_PROVIDER=fake` via `get_provider_config`. |
| T1-CFG-05 | `provider_aliases` | `gemini`/`vertex` → `google_vertexai`, `vertex-claude` → `vertex_anthropic`, `ollama_cloud` → `ollama-cloud`. |
| T1-CFG-06 | `relative_paths_anchor_agent_root` | `sqlite:///x.db`, memory URI resolve against the agent root. |
| T2-CFG-G1 | `reload_hot_key` (gateway) | `POST /admin/config/reload` changing `model.name`: `ConfigReloaded` on every open session; next turn uses the new model. |
| T2-CFG-G2 | `reload_restart_key_reported` | `DB_URL` change: `restart_required` lists it; backend unchanged. |
| T2-CFG-G3 | `reload_mid_turn_pinned` | Reload during a turn: in-flight turn keeps the pinned revision. |
| T2-CFG-G4 | `reload_auth` | Non-loopback rejected; bearer required when `MONKEYBOT_ADMIN_TOKEN` set. |
| T2-CFG-G5 | `bootstrap_memory_failure_continues` | Ordinary memory setup failure: gateway ready without memory; `MemoryConfigurationError` aborts. |
| T4-CFG-01 | `reload_memory_off_live` | Reload `memory.enabled: false`; next live turn has no wake-up block. |
