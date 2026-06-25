"""Load ``monkeybot_config/monkeybot.yaml`` into ``os.environ`` for unset keys only.

Precedence: existing process environment (including ``.env`` via dotenv) wins.
Discovery: ``MONKEYBOT_CONFIG`` if set, else ``<cwd>/monkeybot_config/monkeybot.yaml``.
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
    ("paths", "workspace_root"): "MONKEYBOT_WORKSPACE_ROOT",
    ("model", "provider"): "MODEL_PROVIDER",
    ("model", "name"): "MODEL_NAME",
    ("model", "temperature"): "MODEL_TEMPERATURE",
    ("model", "max_tokens"): "MODEL_MAX_TOKENS",
    ("model", "thinking_budget"): "MODEL_THINKING_BUDGET",
    ("model", "enable_caching"): "MODEL_ENABLE_CACHING",
    ("model", "context_window"): "MODEL_CONTEXT_WINDOW",
    ("model", "summarization_model"): "CONTEXT_SUMMARIZATION_MODEL",
    ("model", "max_turns"): "MAX_TURNS",
    ("gcp", "project_id"): "VERTEX_AI_PROJECT_ID",
    ("gcp", "location"): "VERTEX_AI_LOCATION",
    ("anthropic_vertex", "project_id"): "ANTHROPIC_VERTEX_PROJECT_ID",
    ("anthropic_vertex", "region"): "ANTHROPIC_VERTEX_REGION",
    ("gateway", "pending_response_timeout_sec"): "PENDING_RESPONSE_TIMEOUT_SEC",
    ("gateway", "sse_replay_max"): "SSE_REPLAY_MAX",
    ("gateway", "graceful_shutdown_timeout_sec"): "GRACEFUL_SHUTDOWN_TIMEOUT_SEC",
    ("gateway", "cors_allow_origins"): "MONKEYBOT_CORS_ALLOW_ORIGINS",
    ("context_curation", "enabled"): "CONTEXT_CURATION_ENABLED",
    ("context_curation", "skill_threshold"): "CONTEXT_CURATION_SKILL_THRESHOLD",
    ("context_curation", "memory_threshold"): "CONTEXT_CURATION_MEMORY_THRESHOLD",
    ("context_curation", "curator_model"): "CONTEXT_CURATOR_MODEL",
    ("context_curation", "timeout_sec"): "CONTEXT_CURATION_TIMEOUT_SEC",
    ("context_curation", "max_memory_lines"): "CONTEXT_CURATION_MAX_MEMORY_LINES",
    ("context_curation", "max_skills"): "CONTEXT_CURATION_MAX_SKILLS",
    ("context_curation", "search_max_hits"): "CONTEXT_CURATION_SEARCH_MAX_HITS",
    ("memory_hook", "enabled"): "MONKEYBOT_MEMORY_HOOK_ENABLED",
    ("subagent", "timeout_sec"): "SUBAGENT_TIMEOUT_SEC",
    ("subagent", "max_turns"): "SUBAGENT_MAX_TURNS",
    ("subagent", "agent_md"): "MONKEYBOT_SUBAGENT_AGENT_MD",
    ("tools", "denied_patterns"): "MONKEYBOT_TOOL_DENIED_PATTERNS",
    ("tools", "read_max_lines"): "MONKEYBOT_READ_MAX_LINES",
    ("tools", "read_default_lines"): "MONKEYBOT_READ_DEFAULT_LINES",
    ("tools", "spill_read_max_lines"): "MONKEYBOT_SPILL_READ_MAX_LINES",
    ("tools", "spill_min_chars"): "MONKEYBOT_SPILL_MIN_CHARS",
    ("tools", "result_budget_fraction"): "MONKEYBOT_RESULT_BUDGET_FRACTION",
    ("tools", "result_budget_floor_tokens"): "MONKEYBOT_RESULT_BUDGET_FLOOR_TOKENS",
    ("web_search", "backend"): "WEB_SEARCH_BACKEND",
    ("web_search", "max_results"): "WEB_SEARCH_MAX_RESULTS",
    ("sandbox", "enabled"): "SANDBOX_ENABLED",
    ("sandbox", "server_url"): "SANDBOX_SERVER_URL",
    ("sandbox", "image"): "SANDBOX_IMAGE",
    ("sandbox", "ttl_seconds"): "SANDBOX_TTL_SECONDS",
    ("fake_provider", "events_json"): "MONKEYBOT_FAKE_PROVIDER_EVENTS",
}

# Backward-compatible alias for internal/tests.
_ENV_MAP = ENV_MAP


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


def _flatten_config(data: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for (section, key), env_name in ENV_MAP.items():
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        if key not in sec:
            continue
        raw = sec[key]
        if env_name == "MONKEYBOT_TOOL_DENIED_PATTERNS":
            s = _denied_patterns_to_env(raw)
        else:
            s = _scalar_to_env(raw)
        if s is not None:
            out[env_name] = s
    return out


def _resolve_config_path() -> Path | None:
    explicit = os.environ.get("MONKEYBOT_CONFIG", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p.resolve()
        logger.warning("MONKEYBOT_CONFIG is set but not a file: %s", p)
        return None
    default = Path.cwd() / "monkeybot_config" / "monkeybot.yaml"
    if default.is_file():
        return default.resolve()
    return None


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


def apply_monkeybot_runtime_env() -> Path | None:
    """Apply YAML-backed defaults to ``os.environ`` (unset keys only). Safe to call twice."""
    global _RUNTIME_ENV_APPLIED
    if _RUNTIME_ENV_APPLIED:
        return None

    path = _resolve_config_path()
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

    _RUNTIME_ENV_APPLIED = True
    google_cloud_project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    for env_key, env_val in flat.items():
        if env_key in os.environ:
            continue
        if env_key in _GCP_PROJECT_ENV_KEYS and google_cloud_project:
            os.environ[env_key] = google_cloud_project
            logger.debug(
                "Set from GOOGLE_CLOUD_PROJECT: %s=%s", env_key, google_cloud_project
            )
            continue
        os.environ[env_key] = env_val
        logger.debug("Set from monkeybot.yaml: %s=%s", env_key, env_val)

    logger.info("Applied runtime config from %s (%d keys)", path, len(flat))
    return path
