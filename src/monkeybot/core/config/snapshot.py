"""Immutable, versioned runtime config snapshot and process-wide ``ConfigStore``.

Built behind the existing env surface: ``apply_monkeybot_runtime_env`` fills
unset ``os.environ`` keys for subprocess transport. In-process readers use
:func:`current_env` / :func:`env_value`. Layering happens at build time, never
import time: code defaults, merged YAML, then pinned process env.

Once a snapshot is loaded (the gateway always has one), ``env_value`` does
**not** fall through to ``os.environ``. ``_effective_env`` only contains keys
present in YAML or pinned at first build, plus layout paths overlaid by
``AgentLayout.export_environment``. An ENV_MAP key exported into process env
*after* bootstrap is invisible to in-process readers. Empty store (tests /
pre-apply) still reads ``os.environ``.

Section dataclasses (``ModelConfig``, ``PathsConfig``, …) and content-file
digests land with reload tiers. In-process readers use ``current_env`` /
``env_flag`` against ``env_values``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from monkeybot.core.config.realtime_config import RealtimeConfig, realtime_config_from_doc
from monkeybot.core.config.runtime_env import (
    _GCP_PROJECT_ENV_KEYS,
    ENV_MAP,
    _flatten_config,
    _load_yaml_file,
    _merge_with_includes,
    _resolve_config_path,
    warn_retired_curation_keys,
    warn_retired_tools_keys,
)
from monkeybot.core.config.settings import (
    ConfigError,
    SubagentConfig,
    SubagentSettings,
    _parse_subagent_entries,
    _subagents_section,
)
from monkeybot.core.config.settings import (
    subagent_settings_from_section as _subagent_settings_from_section,
)
from monkeybot.core.layout import (
    resolve_agent_path,
    resolve_agent_root,
    resolve_memory_storage_uri,
    resolve_sqlite_url,
)

# This module imports settings + layout at load time. Those modules (and
# settings' provider imports: gemini, ollama, sampling, vertex_claude,
# llm.provider) plus layout → memory → sqlite must import current_env /
# overlay_env_values lazily.

logger = logging.getLogger(__name__)

_PATH_KEYS = frozenset(
    {
        "AGENT_MD",
        "MEMORY_PATH",
        "SKILLS_PATH",
        "MCP_CONFIG",
        "COMMAND_ALLOWLIST_CONFIG",
        "PERMISSION_CONFIG",
        "MONKEYBOT_WORKSPACE_ROOT",
    }
)

_PINNED_EXTRA_KEYS = (
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GCP_PROJECT_ID",
    "WORKSPACE_ROOT",
)

_PINNED_ENV: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    revision: int
    digest: str  # yaml + pins + overlaid env_values (not yaml+pins alone)
    source_path: Path | None
    loaded_at: float
    realtime: RealtimeConfig
    subagents: Mapping[str, SubagentConfig]
    subagent_settings: SubagentSettings
    env_values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "env_values", MappingProxyType(dict(self.env_values)))
        object.__setattr__(self, "subagents", MappingProxyType(dict(self.subagents)))


class ConfigStore:
    """Process-wide holder of the current ``RuntimeConfig`` pointer."""

    __slots__ = ("_current", "_config_path", "_agent_root", "_lock")

    def __init__(self) -> None:
        self._current: RuntimeConfig | None = None
        self._config_path: Path | None = None
        self._agent_root: Path | None = None
        self._lock = threading.Lock()

    def current(self) -> RuntimeConfig:
        with self._lock:
            cfg = self._current
        if cfg is None:
            raise RuntimeError("RuntimeConfig has not been loaded")
        return cfg

    def current_or_none(self) -> RuntimeConfig | None:
        """Return the current snapshot, or ``None`` when apply has not run."""
        with self._lock:
            return self._current

    def _set(
        self,
        cfg: RuntimeConfig,
        *,
        config_path: Path | None,
        agent_root: Path | None,
    ) -> None:
        with self._lock:
            self._current = cfg
            self._config_path = config_path
            self._agent_root = agent_root

    def _overlay_env(self, updates: Mapping[str, str]) -> None:
        """Merge ``updates`` into ``_current.env_values`` without bumping revision.

        Recomputes digest under the same lock as ``_set``. No-op when the store
        is empty or when ``updates`` would not change any value.
        """
        with self._lock:
            cfg = self._current
            if cfg is None:
                return
            merged = dict(cfg.env_values)
            changed = False
            for key, value in updates.items():
                if merged.get(key) != value:
                    merged[key] = value
                    changed = True
            if not changed:
                return
            self._current = replace(
                cfg, env_values=merged, digest=_digest_with_env(cfg.digest, merged)
            )

    def _reset(self) -> None:
        with self._lock:
            self._current = None
            self._config_path = None
            self._agent_root = None


_PROCESS_STORE = ConfigStore()


def get_config_store() -> ConfigStore:
    """Return the process-wide config store."""
    return _PROCESS_STORE


def env_value(cfg: RuntimeConfig | None, key: str, default: str = "") -> str:
    """Read one ENV_MAP key from a pinned snapshot, else ``os.environ``.

    When ``cfg`` is set, missing snapshot keys return ``default`` and do not
    fall through to process env — including keys exported into ``os.environ``
    after bootstrap. An empty store (``cfg is None``) reads ``os.environ``.
    Layout-resolved paths land in ``env_values`` via ``overlay_env_values``.
    """
    if cfg is not None:
        val = cfg.env_values.get(key)
        return val if val is not None else default
    return os.environ.get(key, default)


def env_flag(cfg: RuntimeConfig | None, key: str, *, default: bool) -> bool:
    """Boolean env flag. Opt-out keys use ``default=True``; opt-in use ``default=False``."""
    raw = env_value(cfg, key, "true" if default else "false").strip().lower()
    if default:
        return raw not in {"0", "false", "no", "off"}
    return raw in {"1", "true", "yes", "on"}


def current_env(key: str, default: str = "") -> str:
    """Read an ENV_MAP key from the process snapshot, else ``os.environ``."""
    return env_value(get_config_store().current_or_none(), key, default)


def current_env_flag(key: str, *, default: bool) -> bool:
    """Boolean ENV_MAP flag from the process snapshot, else ``os.environ``."""
    return env_flag(get_config_store().current_or_none(), key, default=default)


def current_env_or_none(key: str) -> str | None:
    """Like :func:`current_env` but missing keys are ``None`` (not a default)."""
    cfg = get_config_store().current_or_none()
    if cfg is not None:
        return cfg.env_values.get(key)
    return os.environ.get(key)


def context_window_tokens(cfg: RuntimeConfig | None = None, default: int = 200_000) -> int:
    """Effective ``MODEL_CONTEXT_WINDOW`` from a pinned snapshot or process env.

    ``cfg is None`` consults the process store (same as :func:`current_env`),
    unlike :func:`env_value` where ``None`` means empty store / ``os.environ``.
    """
    if cfg is None:
        cfg = get_config_store().current_or_none()
    raw = env_value(cfg, "MODEL_CONTEXT_WINDOW", str(default)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("invalid MODEL_CONTEXT_WINDOW %s", raw)
        return default


def overlay_env_values(updates: Mapping[str, str]) -> None:
    """Merge ``updates`` into the current snapshot without bumping revision.

    Used by ``AgentLayout.export_environment`` so layout-resolved paths
    (``SKILLS_PATH``, ``AGENT_MD``, ``DB_URL``, …) are visible to
    ``current_env`` after bootstrap. No-op when the store is empty.

    This completes the same revision rather than a YAML reload. Digest is
    recomputed over the new ``env_values`` so a cache keyed on digest cannot
    serve stale layout paths. Serialized under the store lock against ``_set``.
    """
    get_config_store()._overlay_env(updates)


def pinned_env_names() -> frozenset[str]:
    """ENV_MAP (and extra) keys captured as pins on first build."""
    if _PINNED_ENV is None:
        return frozenset()
    return frozenset(_PINNED_ENV)


def reset_snapshot_state_for_tests() -> None:
    """Clear the process store and pin capture (tests only)."""
    global _PINNED_ENV
    _PINNED_ENV = None
    _PROCESS_STORE._reset()


def load_into_store(
    *, config_path: Path | None = None, agent_root: Path | None = None
) -> RuntimeConfig:
    """Build a snapshot at revision 1 and install it as the process current config."""
    cfg = build_runtime_config(config_path=config_path, agent_root=agent_root, revision=1)
    _PROCESS_STORE._set(cfg, config_path=config_path, agent_root=agent_root)
    return cfg


def build_runtime_config(
    *,
    config_path: Path | None = None,
    agent_root: Path | None = None,
    revision: int = 0,
) -> RuntimeConfig:
    """Layer defaults, merged YAML, and pinned process env into one snapshot."""
    pinned = _capture_pins()
    source_path, merged = _load_merged_yaml(config_path=config_path, agent_root=agent_root)
    warn_retired_tools_keys(merged)
    warn_retired_curation_keys(merged)
    anchor = agent_root or resolve_agent_root(config_path=source_path)
    env_values = _effective_env(_flatten_config(merged), pinned, anchor)
    digest = _compute_digest(merged=merged, pinned=pinned)
    subagents, subagent_settings = _parse_subagents(merged)
    return RuntimeConfig(
        revision=revision,
        digest=digest,
        source_path=source_path,
        loaded_at=time.time(),
        realtime=realtime_config_from_doc(merged, env_values),
        subagents=subagents,
        subagent_settings=subagent_settings,
        env_values=env_values,
    )


def _capture_pins() -> dict[str, str]:
    global _PINNED_ENV
    if _PINNED_ENV is not None:
        return _PINNED_ENV
    pinned: dict[str, str] = {}
    for env_name in ENV_MAP.values():
        if env_name in os.environ:
            pinned[env_name] = os.environ[env_name]
    for extra in _PINNED_EXTRA_KEYS:
        if extra in os.environ:
            pinned[extra] = os.environ[extra]
    _PINNED_ENV = pinned
    return pinned


def _load_merged_yaml(
    *, config_path: Path | None, agent_root: Path | None
) -> tuple[Path | None, dict[str, Any]]:
    if config_path is not None:
        root = _load_yaml_file(config_path)
        return config_path, _merge_with_includes(config_path, root)
    path = _resolve_config_path(agent_root=agent_root)
    if path is None:
        return None, {}
    root = _load_yaml_file(path)
    return path, _merge_with_includes(path, root)


def _resolve_yaml_value(env_key: str, env_val: str, anchor: Path) -> str:
    if env_key in _PATH_KEYS:
        return str(resolve_agent_path(env_val, anchor))
    if env_key == "DB_URL":
        return resolve_sqlite_url(env_val, anchor)
    if env_key == "MEMORY_STORAGE_URI":
        return resolve_memory_storage_uri(env_val, anchor)
    return env_val


def _effective_env(
    flat: Mapping[str, str], pinned: Mapping[str, str], anchor: Path
) -> dict[str, str]:
    """YAML (unset keys only), then pinned process env. Pins always win."""
    google_cloud_project = (pinned.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    out: dict[str, str] = {}
    for env_key, env_val in flat.items():
        if env_key in pinned:
            continue
        if env_key == "MONKEYBOT_WORKSPACE_ROOT" and pinned.get("WORKSPACE_ROOT"):
            continue
        if env_key in _GCP_PROJECT_ENV_KEYS and google_cloud_project:
            out[env_key] = google_cloud_project
            continue
        out[env_key] = _resolve_yaml_value(env_key, env_val, anchor)
    for env_key in ENV_MAP.values():
        if env_key in pinned:
            out[env_key] = pinned[env_key]
    for extra in _PINNED_EXTRA_KEYS:
        if extra in pinned:
            out[extra] = pinned[extra]
    return out


def _compute_digest(*, merged: Mapping[str, Any], pinned: Mapping[str, str]) -> str:
    payload = {
        "yaml": merged,
        "pinned": {k: pinned[k] for k in sorted(pinned)},
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _digest_with_env(base_digest: str, env_values: Mapping[str, str]) -> str:
    """Fold ``env_values`` into a YAML+pins digest so overlay invalidates caches."""
    payload = {"base": base_digest, "env": {k: env_values[k] for k in sorted(env_values)}}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _parse_subagents(
    doc: Mapping[str, Any],
) -> tuple[dict[str, SubagentConfig], SubagentSettings]:
    """Parse ``subagents:`` for the snapshot.

    Invalid or legacy shapes (bare list, bad types) warn and fall back to
    defaults so a malformed section cannot abort ``apply_monkeybot_runtime_env``
    / ``bootstrap_agent_layout``. ``get_subagent_settings`` still raises when
    the task tool actually asks for subagents.
    """
    try:
        section = _subagents_section(dict(doc))
        settings = _subagent_settings_from_section(section)
    except ConfigError as exc:
        logger.warning("Ignoring invalid subagents section during snapshot load: %s", exc)
        return {}, SubagentSettings()
    personas: dict[str, SubagentConfig] = {}
    for cfg in _parse_subagent_entries(section.get("personas")):
        personas[cfg.name] = cfg
    return personas, settings


__all__ = [
    "ConfigStore",
    "RuntimeConfig",
    "build_runtime_config",
    "current_env",
    "current_env_flag",
    "current_env_or_none",
    "context_window_tokens",
    "env_flag",
    "env_value",
    "get_config_store",
    "load_into_store",
    "overlay_env_values",
    "pinned_env_names",
    "reset_snapshot_state_for_tests",
]
