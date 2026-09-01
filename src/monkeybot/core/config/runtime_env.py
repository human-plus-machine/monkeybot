"""Load ``monkeybot_config/monkeybot.yaml`` into ``os.environ`` for unset keys only.

Precedence: existing process environment (including ``.env`` via dotenv) wins
for ``ENV_MAP`` keys except ``YAML_ONLY_ENV_KEYS`` (``model.*``), which are
YAML-only — leftover process env is ignored (and warned once). If leftover
``MODEL_PROVIDER`` is set and YAML has no ``model.provider``, load fails.
Discovery: ``MONKEYBOT_CONFIG`` if set, else the nearest parent directory with
``monkeybot_config/monkeybot.yaml``. Relative ``paths.*`` values are anchored
at the agent root, never the process working directory.
Optional ``includes``: list of paths relative to the primary config file's directory.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# (section, key) -> os.environ name
# YAML gcp/anthropic_vertex project ids map here; prefer GOOGLE_CLOUD_PROJECT when set in .env.
_GCP_PROJECT_ENV_KEYS = frozenset({"VERTEX_AI_PROJECT_ID", "ANTHROPIC_VERTEX_PROJECT_ID"})

# Public alias for CLI / validation (stable API).
ENV_MAP: dict[tuple[str, str], str] = {
    ("runtime", "log_level"): "LOG_LEVEL",
    ("runtime", "port"): "PORT",
    ("runtime", "gateway_port"): "GATEWAY_PORT",
    ("paths", "agent_md"): "AGENT_MD",
    ("paths", "memory_path"): "MEMORY_PATH",
    ("paths", "memory_storage_uri"): "MEMORY_STORAGE_URI",
    ("paths", "skills_path"): "SKILLS_PATH",
    ("paths", "db_url"): "DB_URL",
    ("paths", "mcp_config"): "MCP_CONFIG",
    ("paths", "command_allowlist_config"): "COMMAND_ALLOWLIST_CONFIG",
    ("paths", "permission_config"): "PERMISSION_CONFIG",
    ("paths", "workspace_root"): "MONKEYBOT_WORKSPACE_ROOT",
    ("paths", "agent_id"): "MONKEYBOT_AGENT_ID",
    ("model", "provider"): "MODEL_PROVIDER",
    ("model", "name"): "MODEL_NAME",
    ("model", "temperature"): "MODEL_TEMPERATURE",
    ("model", "max_tokens"): "MODEL_MAX_TOKENS",
    ("model", "thinking_budget"): "MODEL_THINKING_BUDGET",
    ("model", "context_window"): "MODEL_CONTEXT_WINDOW",
    ("model", "summarization_model"): "CONTEXT_SUMMARIZATION_MODEL",
    ("model", "max_turns"): "MAX_TURNS",
    ("model", "cache_retention"): "MODEL_CACHE_RETENTION",
    ("gcp", "project_id"): "VERTEX_AI_PROJECT_ID",
    ("gcp", "location"): "VERTEX_AI_LOCATION",
    ("anthropic_vertex", "project_id"): "ANTHROPIC_VERTEX_PROJECT_ID",
    ("anthropic_vertex", "region"): "ANTHROPIC_VERTEX_REGION",
    ("gateway", "pending_response_timeout_sec"): "PENDING_RESPONSE_TIMEOUT_SEC",
    ("gateway", "sse_replay_max"): "SSE_REPLAY_MAX",
    ("gateway", "sse_nested_replay_max"): "SSE_NESTED_REPLAY_MAX",
    ("gateway", "graceful_shutdown_timeout_sec"): "GRACEFUL_SHUTDOWN_TIMEOUT_SEC",
    ("gateway", "cors_allow_origins"): "MONKEYBOT_CORS_ALLOW_ORIGINS",
    ("context_curation", "enabled"): "CONTEXT_CURATION_ENABLED",
    ("context_curation", "memory_window_lines"): "CONTEXT_CURATION_MEMORY_WINDOW_LINES",
    ("context_curation", "memory_index_cap"): "MEMORY_INDEX_CAP",
    ("context_curation", "memory_token_threshold"): "CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD",
    ("memory", "enabled"): "MONKEYBOT_MEMORY_HOOK_ENABLED",
    ("memory", "backend"): "MEMPALACE_BACKEND",
    ("memory", "embedding_model"): "MEMPALACE_EMBEDDING_MODEL",
    ("tools", "denied_patterns"): "MONKEYBOT_TOOL_DENIED_PATTERNS",
    ("compression", "resume_thinking_budget"): "MONKEYBOT_RESUME_THINKING_BUDGET",
    ("web_search", "backend"): "WEB_SEARCH_BACKEND",
    ("web_search", "max_results"): "WEB_SEARCH_MAX_RESULTS",
    ("todo_list", "enabled"): "MONKEYBOT_TODO_LIST_ENABLED",
    ("todo_list", "mirror_to_disk"): "MONKEYBOT_TODO_LIST_MIRROR_TO_DISK",
    ("computer", "enabled"): "MONKEYBOT_COMPUTER_TOOLS",
    ("paths", "approvals_config"): "MONKEYBOT_APPROVALS_CONFIG",
    ("sandbox", "enabled"): "SANDBOX_ENABLED",
    ("sandbox", "server_url"): "SANDBOX_SERVER_URL",
    ("sandbox", "image"): "SANDBOX_IMAGE",
    ("sandbox", "ttl_seconds"): "SANDBOX_TTL_SECONDS",
    ("sandbox", "shared_filesystem"): "SANDBOX_SHARED_FILESYSTEM",
    ("scheduler", "enabled"): "MONKEYBOT_SCHEDULER_ENABLED",
    ("fake_provider", "events_json"): "MONKEYBOT_FAKE_PROVIDER_EVENTS",
    ("emission", "style"): "MONKEYBOT_EMISSION_STYLE",
    ("harness", "mode"): "MONKEYBOT_HARNESS_MODE",
    ("realtime", "websocket.enabled"): "MONKEYBOT_REALTIME_WS_ENABLED",
    ("realtime", "websocket.port"): "MONKEYBOT_REALTIME_WS_PORT",
    ("realtime", "audio.input_format"): "MONKEYBOT_REALTIME_AUDIO_INPUT_FORMAT",
    ("realtime", "audio.output_format"): "MONKEYBOT_REALTIME_AUDIO_OUTPUT_FORMAT",
    ("realtime", "audio.chunk_ms"): "MONKEYBOT_REALTIME_AUDIO_CHUNK_MS",
    ("realtime", "audio.max_utterance_sec"): "MONKEYBOT_REALTIME_AUDIO_MAX_UTTERANCE_SEC",
    ("realtime", "session.max_duration_sec"): "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC",
    ("realtime", "session.idle_timeout_sec"): "MONKEYBOT_REALTIME_SESSION_IDLE_TIMEOUT_SEC",
    (
        "realtime",
        "session.max_response_turn_sec",
    ): "MONKEYBOT_REALTIME_SESSION_MAX_RESPONSE_TURN_SEC",
    (
        "realtime",
        "session.max_concurrent_sessions",
    ): "MONKEYBOT_REALTIME_SESSION_MAX_CONCURRENT_SESSIONS",
    (
        "realtime",
        "metrics.emit_summary_on_close",
    ): "MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE",
}

# Flattened into snapshot ``env_values`` but never pinned from process env and
# never written back to ``os.environ``. Leftover values are warned and ignored.
# Derived from ENV_MAP so a new model.* key cannot silently regain env override.
YAML_ONLY_ENV_KEYS: frozenset[str] = frozenset(
    env for (section, _), env in ENV_MAP.items() if section == "model"
)
_yaml_only_model_env_warned = False

# Backward-compatible alias for internal/tests.
_ENV_MAP = ENV_MAP


class ConfigTier(StrEnum):
    """Reload action for one ``ENV_MAP`` key. Every mapped key has exactly one tier."""

    HOT = "hot"
    REBUILD = "rebuild"
    RECONNECT_MCP = "reconnect_mcp"
    RESTART = "restart"


# env var -> (reload tier, dotted RuntimeConfig path). Must cover ``ENV_MAP`` 1:1
# (enforced in tests). One map so a new setting cannot get a path without a tier.
ENV_SPEC: dict[str, tuple[ConfigTier, str]] = {
    # HOT — next turn reads the new snapshot.
    "MODEL_NAME": (ConfigTier.HOT, "model.name"),
    "MAX_TURNS": (ConfigTier.HOT, "model.max_turns"),
    "MODEL_CONTEXT_WINDOW": (ConfigTier.HOT, "model.context_window"),
    "CONTEXT_SUMMARIZATION_MODEL": (ConfigTier.HOT, "model.summarization_model"),
    "MODEL_CACHE_RETENTION": (ConfigTier.HOT, "model.cache_retention"),
    "CONTEXT_CURATION_ENABLED": (ConfigTier.HOT, "curation.enabled"),
    "CONTEXT_CURATION_MEMORY_WINDOW_LINES": (ConfigTier.HOT, "curation.memory_window_lines"),
    "MEMORY_INDEX_CAP": (ConfigTier.HOT, "curation.memory_index_cap"),
    "CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD": (
        ConfigTier.HOT,
        "curation.memory_token_threshold",
    ),
    "MONKEYBOT_TODO_LIST_ENABLED": (ConfigTier.HOT, "tools.todo_list_enabled"),
    "MONKEYBOT_TODO_LIST_MIRROR_TO_DISK": (ConfigTier.HOT, "tools.todo_list_mirror_to_disk"),
    "MONKEYBOT_EMISSION_STYLE": (ConfigTier.HOT, "gateway.emission_style"),
    "LOG_LEVEL": (ConfigTier.HOT, "gateway.log_level"),
    "AGENT_MD": (ConfigTier.HOT, "paths.agent_md"),
    "SKILLS_PATH": (ConfigTier.HOT, "paths.skills_path"),
    "MONKEYBOT_RESUME_THINKING_BUDGET": (ConfigTier.HOT, "tools.resume_thinking_budget"),
    # REBUILD — swap a GatewayRuntime slice (provider, inspectors, web search, memory hook).
    "MODEL_PROVIDER": (ConfigTier.REBUILD, "model.provider"),
    "MODEL_TEMPERATURE": (ConfigTier.REBUILD, "model.temperature"),
    "MODEL_MAX_TOKENS": (ConfigTier.REBUILD, "model.max_tokens"),
    "MODEL_THINKING_BUDGET": (ConfigTier.REBUILD, "model.thinking_budget"),
    "COMMAND_ALLOWLIST_CONFIG": (ConfigTier.REBUILD, "paths.command_allowlist_config"),
    "PERMISSION_CONFIG": (ConfigTier.REBUILD, "paths.permission_config"),
    "WEB_SEARCH_BACKEND": (ConfigTier.REBUILD, "tools.web_search_backend"),
    "WEB_SEARCH_MAX_RESULTS": (ConfigTier.REBUILD, "tools.web_search_max_results"),
    "MONKEYBOT_MEMORY_HOOK_ENABLED": (ConfigTier.REBUILD, "memory.enabled"),
    "VERTEX_AI_PROJECT_ID": (ConfigTier.REBUILD, "model.vertex_project_id"),
    "VERTEX_AI_LOCATION": (ConfigTier.REBUILD, "model.vertex_location"),
    "ANTHROPIC_VERTEX_PROJECT_ID": (ConfigTier.REBUILD, "model.anthropic_vertex_project_id"),
    "ANTHROPIC_VERTEX_REGION": (ConfigTier.REBUILD, "model.anthropic_vertex_region"),
    "PENDING_RESPONSE_TIMEOUT_SEC": (ConfigTier.REBUILD, "gateway.pending_response_timeout_sec"),
    "MONKEYBOT_TOOL_DENIED_PATTERNS": (ConfigTier.REBUILD, "tools.denied_patterns"),
    "MONKEYBOT_COMPUTER_TOOLS": (ConfigTier.REBUILD, "tools.computer_enabled"),
    "MONKEYBOT_APPROVALS_CONFIG": (ConfigTier.REBUILD, "paths.approvals_config"),
    "SANDBOX_ENABLED": (ConfigTier.REBUILD, "tools.sandbox_enabled"),
    "SANDBOX_SERVER_URL": (ConfigTier.REBUILD, "tools.sandbox_server_url"),
    "SANDBOX_IMAGE": (ConfigTier.REBUILD, "tools.sandbox_image"),
    "SANDBOX_TTL_SECONDS": (ConfigTier.REBUILD, "tools.sandbox_ttl_seconds"),
    "SANDBOX_SHARED_FILESYSTEM": (ConfigTier.REBUILD, "tools.sandbox_shared_filesystem"),
    "MONKEYBOT_SCHEDULER_ENABLED": (ConfigTier.REBUILD, "tools.scheduler_enabled"),
    "MONKEYBOT_FAKE_PROVIDER_EVENTS": (ConfigTier.REBUILD, "model.fake_provider_events"),
    # RECONNECT_MCP — diff-based MCP reconnect; path *or* file content.
    "MCP_CONFIG": (ConfigTier.RECONNECT_MCP, "paths.mcp_config"),
    # RESTART — do not attempt in-process (identity, bind, storage, realtime).
    "DB_URL": (ConfigTier.RESTART, "paths.db_url"),
    "MONKEYBOT_WORKSPACE_ROOT": (ConfigTier.RESTART, "paths.workspace_root"),
    "MONKEYBOT_AGENT_ID": (ConfigTier.RESTART, "paths.agent_id"),
    "MEMORY_STORAGE_URI": (ConfigTier.RESTART, "paths.memory_storage_uri"),
    "MEMORY_PATH": (ConfigTier.RESTART, "paths.memory_path"),
    "PORT": (ConfigTier.RESTART, "gateway.port"),
    "GATEWAY_PORT": (ConfigTier.RESTART, "gateway.gateway_port"),
    "SSE_REPLAY_MAX": (ConfigTier.RESTART, "gateway.sse_replay_max"),
    "SSE_NESTED_REPLAY_MAX": (ConfigTier.RESTART, "gateway.sse_nested_replay_max"),
    "GRACEFUL_SHUTDOWN_TIMEOUT_SEC": (ConfigTier.RESTART, "gateway.graceful_shutdown_timeout_sec"),
    "MONKEYBOT_CORS_ALLOW_ORIGINS": (ConfigTier.RESTART, "gateway.cors_allow_origins"),
    "MEMPALACE_BACKEND": (ConfigTier.RESTART, "memory.backend"),
    "MEMPALACE_EMBEDDING_MODEL": (ConfigTier.RESTART, "memory.embedding_model"),
    "MONKEYBOT_HARNESS_MODE": (ConfigTier.RESTART, "gateway.harness_mode"),
    "MONKEYBOT_REALTIME_WS_ENABLED": (ConfigTier.RESTART, "realtime.websocket.enabled"),
    "MONKEYBOT_REALTIME_WS_PORT": (ConfigTier.RESTART, "realtime.websocket.port"),
    "MONKEYBOT_REALTIME_AUDIO_INPUT_FORMAT": (ConfigTier.RESTART, "realtime.audio.input_format"),
    "MONKEYBOT_REALTIME_AUDIO_OUTPUT_FORMAT": (ConfigTier.RESTART, "realtime.audio.output_format"),
    "MONKEYBOT_REALTIME_AUDIO_CHUNK_MS": (ConfigTier.RESTART, "realtime.audio.chunk_ms"),
    "MONKEYBOT_REALTIME_AUDIO_MAX_UTTERANCE_SEC": (
        ConfigTier.RESTART,
        "realtime.audio.max_utterance_sec",
    ),
    "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC": (
        ConfigTier.RESTART,
        "realtime.session.max_duration_sec",
    ),
    "MONKEYBOT_REALTIME_SESSION_IDLE_TIMEOUT_SEC": (
        ConfigTier.RESTART,
        "realtime.session.idle_timeout_sec",
    ),
    "MONKEYBOT_REALTIME_SESSION_MAX_RESPONSE_TURN_SEC": (
        ConfigTier.RESTART,
        "realtime.session.max_response_turn_sec",
    ),
    "MONKEYBOT_REALTIME_SESSION_MAX_CONCURRENT_SESSIONS": (
        ConfigTier.RESTART,
        "realtime.session.max_concurrent_sessions",
    ),
    "MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE": (
        ConfigTier.RESTART,
        "realtime.metrics.emit_summary_on_close",
    ),
}

ENV_TIERS: dict[str, ConfigTier] = {name: spec[0] for name, spec in ENV_SPEC.items()}
ENV_FIELD_PATHS: dict[str, str] = {name: spec[1] for name, spec in ENV_SPEC.items()}

# Content-addressed files tracked by digest, mapped to the ENV_MAP key whose tier applies.
CONTENT_DIGEST_TIERS: dict[str, str] = {
    "agent_md": "AGENT_MD",
    "skills": "SKILLS_PATH",
    "mcp_config": "MCP_CONFIG",
    "command_allowlist": "COMMAND_ALLOWLIST_CONFIG",
    "permission_config": "PERMISSION_CONFIG",
}

# Synthetic diff key for ``subagents.personas`` (not in ENV_MAP). Always REBUILD.
SUBAGENTS_DIFF_KEY = "subagents.*"

# YAML keys that used to map to env but are retired. Still accepted in the file
# (no unknown-key rejection), but warn so configs are not silently ignored.
RETIRED_TOOLS_KEYS: frozenset[str] = frozenset(
    {
        "spill_min_chars",
        "spill_read_max_lines",
        "read_default_lines",
    }
)
RETIRED_CONTEXT_CURATION_KEYS: frozenset[str] = frozenset(
    {
        "curator_model",
        "timeout_sec",
    }
)

_SPILL_SIZING_RETIRED_MSG = (
    "tools.{key} is retired and ignored — spill/read sizing is derived from "
    "model.context_window (see docs/spill-dynamic-design.md)"
)

# Per-key overrides for keys whose replacement isn't the shared spill-sizing message.
_RETIRED_TOOLS_WARNINGS_OVERRIDES: dict[str, str] = {
    "read_default_lines": (
        "tools.read_default_lines is retired and ignored — read_file defaults to 2000 lines; "
        "pass limit to request more"
    ),
}

def warn_retired_tools_keys(doc: Mapping[str, Any]) -> list[str]:
    """Log one warning per retired YAML key; return the keys found."""
    found: list[str] = []
    tools = doc.get("tools")
    if isinstance(tools, dict):
        for key in sorted(RETIRED_TOOLS_KEYS):
            if key in tools:
                found.append(key)
                message = _RETIRED_TOOLS_WARNINGS_OVERRIDES.get(
                    key
                ) or _SPILL_SIZING_RETIRED_MSG.format(key=key)
                logger.warning(message)
    runtime = doc.get("runtime")
    if isinstance(runtime, dict) and "transcript_include_live" in runtime:
        found.append("transcript_include_live")
        logger.warning(
            "runtime.transcript_include_live is retired and ignored — "
            "transcript capture is runtime.transcript_enabled only"
        )
    return found


def warn_retired_curation_keys(doc: Mapping[str, Any]) -> list[str]:
    """Log one warning per retired ``context_curation.*`` key; return the keys found."""
    section = doc.get("context_curation")
    if not isinstance(section, dict):
        return []
    found: list[str] = []
    for key in sorted(RETIRED_CONTEXT_CURATION_KEYS):
        if key in section:
            found.append(key)
            logger.warning(
                "context_curation.%s is retired and ignored — the LLM curator "
                "was removed; window and token knobs still apply",
                key,
            )
    return found


def check_yaml_only_model_env(merged: Mapping[str, Any] | None = None) -> list[str]:
    """Ignore leftover YAML-only model env; fail load if provider is env-only.

    Warns once when process env still has retired model overlay keys. Leftover
    ``MODEL_PROVIDER`` with no YAML ``model.provider`` raises ``ConfigError``.
    """
    global _yaml_only_model_env_warned
    found = [name for name in sorted(YAML_ONLY_ENV_KEYS) if name in os.environ]
    if not found:
        return []
    model = merged.get("model") if isinstance(merged, Mapping) else None
    yaml_provider = model.get("provider") if isinstance(model, dict) else None
    if "MODEL_PROVIDER" in found and not (
        isinstance(yaml_provider, str) and yaml_provider.strip()
    ):
        from monkeybot.core.config.settings import ConfigError

        raise ConfigError(
            "YAML-only MODEL_PROVIDER is set but monkeybot.yaml has no model.provider. "
            "Leftover MODEL_* env is ignored; set model.provider in monkeybot.yaml "
            f"(also set: {', '.join(found)})."
        )
    if not _yaml_only_model_env_warned:
        _yaml_only_model_env_warned = True
        logger.warning(
            "ignoring YAML-only model env: %s — set model.* in monkeybot.yaml instead",
            ",".join(found),
        )
    return found


def reset_runtime_env_state_for_tests() -> None:
    """Clear the process ConfigStore and pin capture (tests only)."""
    global _yaml_only_model_env_warned
    from monkeybot.core.config.settings import reset_transcript_enabled_cache_for_tests
    from monkeybot.core.config.snapshot import reset_snapshot_state_for_tests

    _yaml_only_model_env_warned = False
    reset_snapshot_state_for_tests()
    reset_transcript_enabled_cache_for_tests()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _scalar_to_env(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _denied_patterns_to_env(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return ",".join(parts)
    return None


def _get_nested(mapping: Mapping[str, Any], dotted_key: str) -> Any:
    current: Any = mapping
    for part in dotted_key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _flatten_config(data: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for (section, key), env_name in ENV_MAP.items():
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        raw = _get_nested(sec, key)
        if raw is None:
            continue
        if env_name == "MONKEYBOT_TOOL_DENIED_PATTERNS":
            s = _denied_patterns_to_env(raw)
        else:
            s = _scalar_to_env(raw)
        if s is not None:
            out[env_name] = s
    return out


def _resolve_config_path(*, agent_root: Path | None = None) -> Path | None:
    explicit = os.environ.get("MONKEYBOT_CONFIG", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute() and agent_root is not None:
            p = agent_root / p
        if p.is_file():
            return p.resolve()
        logger.warning("MONKEYBOT_CONFIG is set but not a file: %s", p)
        return None
    if agent_root is None:
        from monkeybot.core.layout import resolve_agent_root

        agent_root = resolve_agent_root()
    default = agent_root / "monkeybot_config" / "monkeybot.yaml"
    return default.resolve() if default.is_file() else None


def _load_yaml_file(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping at root of {path}, got {type(loaded).__name__}")
    return loaded


def _merge_with_includes(primary_path: Path, root: dict[str, Any]) -> dict[str, Any]:
    raw_includes = root.get("includes")
    base = {k: v for k, v in root.items() if k != "includes"}
    if not raw_includes:
        return base
    if not isinstance(raw_includes, list):
        logger.warning("includes: must be a list of paths; ignoring")
        return base
    merged: dict[str, Any] = dict(base)
    parent = primary_path.parent
    for item in raw_includes:
        if not isinstance(item, str) or not item.strip():
            continue
        inc_path = (parent / item.strip()).resolve()
        if not inc_path.is_file():
            logger.warning("include not found, skipping: %s", inc_path)
            continue
        try:
            inc_doc = _load_yaml_file(inc_path)
        except Exception as exc:
            logger.warning("failed to load include %s: %s", inc_path, exc)
            continue
        inc_body = {k: v for k, v in inc_doc.items() if k != "includes"}
        merged = _deep_merge(merged, inc_body)
    return merged


def apply_monkeybot_runtime_env(
    *, config_path: Path | None = None, agent_root: Path | None = None
) -> Path | None:
    """Apply YAML-backed defaults to ``os.environ`` (unset ``ENV_MAP`` keys only).

    Always (re)loads the process ``RuntimeConfig`` snapshot. Unchanged files are
    a digest no-op (same revision). Already-set env keys are never overwritten —
    ``os.environ`` stays the subprocess transport, not the in-process store.
    YAML-only ``model.*`` keys stay in the snapshot and are not copied to process env.
    """
    from monkeybot.core.config.snapshot import (
        get_config_store,
        load_into_store,
        pinned_env_names,
    )

    store = get_config_store()
    try:
        cfg = store.current_or_none()
        if cfg is None:
            cfg = load_into_store(config_path=config_path, agent_root=agent_root)
        else:
            _warn_if_config_path_ignored(cfg.source_path, config_path)
            cfg, _diff = store.reload()
    except Exception as exc:
        path = config_path or _resolve_config_path(agent_root=agent_root)
        logger.error("Failed to load %s: %s", path, exc)
        raise

    google_cloud_project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    pins = pinned_env_names()
    yaml_keys = [
        env_key
        for env_key in cfg.env_values
        if env_key not in pins and env_key not in YAML_ONLY_ENV_KEYS
    ]
    for env_key in yaml_keys:
        env_val = cfg.env_values[env_key]
        # Existing process env wins; WORKSPACE_ROOT blocks YAML workspace_root.
        if env_key in os.environ or (
            env_key == "MONKEYBOT_WORKSPACE_ROOT" and os.environ.get("WORKSPACE_ROOT")
        ):
            continue
        os.environ[env_key] = env_val
        if env_key in _GCP_PROJECT_ENV_KEYS and google_cloud_project:
            logger.debug("Set from GOOGLE_CLOUD_PROJECT: %s=%s", env_key, google_cloud_project)
        else:
            logger.debug("Set from monkeybot.yaml: %s=%s", env_key, env_val)

    if cfg.source_path is None:
        logger.debug(
            "No monkeybot.yaml found (MONKEYBOT_CONFIG / monkeybot_config/monkeybot.yaml); skipping"
        )
        return None

    logger.info("Applied runtime config from %s (%d keys)", cfg.source_path, len(yaml_keys))
    return cfg.source_path


def _warn_if_config_path_ignored(loaded: Path | None, config_path: Path | None) -> None:
    """Warn when a later apply asks for a different YAML than the loaded snapshot."""
    if config_path is None:
        return
    requested = config_path.expanduser().resolve()
    resolved = loaded.resolve() if loaded is not None else None
    if resolved == requested:
        return
    logger.warning(
        "RuntimeConfig already loaded from %s; ignoring %s",
        resolved if resolved is not None else "<none>",
        requested,
    )
