"""Load ``monkeybot_config/monkeybot.yaml`` into ``os.environ`` for unset keys only.

Precedence: existing process environment (including ``.env`` via dotenv) wins.
Discovery: ``MONKEYBOT_CONFIG`` if set, else the nearest parent directory with
``monkeybot_config/monkeybot.yaml``. Relative ``paths.*`` values are anchored
at the agent root, never the process working directory.
Optional ``includes``: list of paths relative to the primary config file's directory.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_RUNTIME_ENV_APPLIED = False

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
    ("context_curation", "curator_model"): "CONTEXT_CURATOR_MODEL",
    ("context_curation", "timeout_sec"): "CONTEXT_CURATION_TIMEOUT_SEC",
    ("memory", "enabled"): "MONKEYBOT_MEMORY_HOOK_ENABLED",
    ("memory", "backend"): "MEMPALACE_BACKEND",
    ("memory", "embedding_model"): "MEMPALACE_EMBEDDING_MODEL",
    ("tools", "denied_patterns"): "MONKEYBOT_TOOL_DENIED_PATTERNS",
    ("compression", "resume_thinking_budget"): "MONKEYBOT_RESUME_THINKING_BUDGET",
    ("web_search", "backend"): "WEB_SEARCH_BACKEND",
    ("web_search", "max_results"): "WEB_SEARCH_MAX_RESULTS",
    ("todo_list", "enabled"): "MONKEYBOT_TODO_LIST_ENABLED",
    ("todo_list", "mirror_to_disk"): "MONKEYBOT_TODO_LIST_MIRROR_TO_DISK",
    ("sandbox", "enabled"): "SANDBOX_ENABLED",
    ("sandbox", "server_url"): "SANDBOX_SERVER_URL",
    ("sandbox", "image"): "SANDBOX_IMAGE",
    ("sandbox", "ttl_seconds"): "SANDBOX_TTL_SECONDS",
    ("sandbox", "shared_filesystem"): "SANDBOX_SHARED_FILESYSTEM",
    ("scheduler", "enabled"): "MONKEYBOT_SCHEDULER_ENABLED",
    ("fake_provider", "events_json"): "MONKEYBOT_FAKE_PROVIDER_EVENTS",
    ("emission", "style"): "MONKEYBOT_EMISSION_STYLE",
    ("runtime", "transcript_enabled"): "MONKEYBOT_TRANSCRIPT_ENABLED",
    ("runtime", "transcript_include_live"): "MONKEYBOT_TRANSCRIPT_INCLUDE_LIVE",
    ("harness", "mode"): "MONKEYBOT_HARNESS_MODE",
    ("realtime", "websocket.enabled"): "MONKEYBOT_REALTIME_WS_ENABLED",
    ("realtime", "websocket.port"): "MONKEYBOT_REALTIME_WS_PORT",
    ("realtime", "audio.input_format"): "MONKEYBOT_REALTIME_AUDIO_INPUT_FORMAT",
    ("realtime", "audio.output_format"): "MONKEYBOT_REALTIME_AUDIO_OUTPUT_FORMAT",
    ("realtime", "audio.chunk_ms"): "MONKEYBOT_REALTIME_AUDIO_CHUNK_MS",
    ("realtime", "audio.max_utterance_sec"): "MONKEYBOT_REALTIME_AUDIO_MAX_UTTERANCE_SEC",
    ("realtime", "session.max_duration_sec"): "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC",
    ("realtime", "session.idle_timeout_sec"): "MONKEYBOT_REALTIME_SESSION_IDLE_TIMEOUT_SEC",
    ("realtime", "session.max_response_turn_sec"): "MONKEYBOT_REALTIME_SESSION_MAX_RESPONSE_TURN_SEC",
    ("realtime", "session.max_concurrent_sessions"): "MONKEYBOT_REALTIME_SESSION_MAX_CONCURRENT_SESSIONS",
    ("realtime", "metrics.emit_summary_on_close"): "MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE",
}

# Backward-compatible alias for internal/tests.
_ENV_MAP = ENV_MAP

# YAML keys that used to map to env but are retired. Still accepted in the file
# (no unknown-key rejection), but warn so configs are not silently ignored.
RETIRED_TOOLS_KEYS: frozenset[str] = frozenset(
    {
        "spill_min_chars",
        "spill_read_max_lines",
        "read_default_lines",
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
    """Log one warning per retired ``tools.*`` key; return the keys found."""
    tools = doc.get("tools")
    if not isinstance(tools, dict):
        return []
    found: list[str] = []
    for key in sorted(RETIRED_TOOLS_KEYS):
        if key in tools:
            found.append(key)
            message = _RETIRED_TOOLS_WARNINGS_OVERRIDES.get(key) or _SPILL_SIZING_RETIRED_MSG.format(key=key)
            logger.warning(message)
    return found


def reset_runtime_env_state_for_tests() -> None:
    """Allow a second ``apply_monkeybot_runtime_env`` in the same interpreter (tests only)."""
    global _RUNTIME_ENV_APPLIED
    _RUNTIME_ENV_APPLIED = False


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
    """Apply YAML-backed defaults to ``os.environ`` (unset keys only). Safe to call twice."""
    global _RUNTIME_ENV_APPLIED
    if _RUNTIME_ENV_APPLIED:
        return None

    path = config_path or _resolve_config_path(agent_root=agent_root)
    if path is None:
        _RUNTIME_ENV_APPLIED = True
        logger.debug(
            "No monkeybot.yaml found (MONKEYBOT_CONFIG / monkeybot_config/monkeybot.yaml); skipping"
        )
        return None

    try:
        root = _load_yaml_file(path)
        merged = _merge_with_includes(path, root)
        flat = _flatten_config(merged)
    except Exception as exc:
        logger.error("Failed to load %s: %s", path, exc)
        raise

    warn_retired_tools_keys(merged)

    _RUNTIME_ENV_APPLIED = True
    google_cloud_project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    from monkeybot.core.layout import (
        resolve_agent_path,
        resolve_agent_root,
        resolve_memory_storage_uri,
        resolve_sqlite_url,
    )

    anchor = agent_root or resolve_agent_root(config_path=path)
    for env_key, env_val in flat.items():
        # WORKSPACE_ROOT remains a supported legacy process override.  Do not
        # let a YAML default materialize MONKEYBOT_WORKSPACE_ROOT ahead of it.
        if env_key in os.environ or (
            env_key == "MONKEYBOT_WORKSPACE_ROOT" and os.environ.get("WORKSPACE_ROOT")
        ):
            continue
        if env_key in _GCP_PROJECT_ENV_KEYS and google_cloud_project:
            os.environ[env_key] = google_cloud_project
            logger.debug(
                "Set from GOOGLE_CLOUD_PROJECT: %s=%s", env_key, google_cloud_project
            )
            continue
        if env_key in {
            "AGENT_MD",
            "MEMORY_PATH",
            "SKILLS_PATH",
            "MCP_CONFIG",
            "COMMAND_ALLOWLIST_CONFIG",
            "PERMISSION_CONFIG",
            "MONKEYBOT_WORKSPACE_ROOT",
        }:
            env_val = str(resolve_agent_path(env_val, anchor))
        elif env_key == "DB_URL":
            env_val = resolve_sqlite_url(env_val, anchor)
        elif env_key == "MEMORY_STORAGE_URI":
            env_val = resolve_memory_storage_uri(env_val, anchor)
        os.environ[env_key] = env_val
        logger.debug("Set from monkeybot.yaml: %s=%s", env_key, env_val)

    logger.info("Applied runtime config from %s (%d keys)", path, len(flat))
    return path
