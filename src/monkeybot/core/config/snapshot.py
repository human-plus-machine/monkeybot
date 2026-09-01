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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from monkeybot.core.config.realtime_config import RealtimeConfig, realtime_config_from_doc
from monkeybot.core.config.runtime_env import (
    _GCP_PROJECT_ENV_KEYS,
    CONTENT_DIGEST_TIERS,
    ENV_FIELD_PATHS,
    ENV_MAP,
    ENV_TIERS,
    SUBAGENTS_DIFF_KEY,
    YAML_ONLY_ENV_KEYS,
    ConfigTier,
    _flatten_config,
    _load_yaml_file,
    _merge_with_includes,
    _resolve_config_path,
    check_yaml_only_model_env,
    warn_retired_curation_keys,
    warn_retired_tools_keys,
)
from monkeybot.core.config.settings import (
    ConfigError,
    SubagentConfig,
    SubagentSettings,
    _parse_subagent_entries,
    _persona_registry,
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
from monkeybot.core.logging_utils import kv

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

_DEFAULT_CONTENT_PATHS: dict[str, str] = {
    "AGENT_MD": "monkeybot_config/AGENT.md",
    "SKILLS_PATH": "skills",
    "MCP_CONFIG": "monkeybot_config/mcp.json",
    "COMMAND_ALLOWLIST_CONFIG": "monkeybot_config/command_allowlist.yaml",
    "PERMISSION_CONFIG": "monkeybot_config/permissions.yaml",
}

_PINNED_ENV: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    provider: str | None = None
    name: str | None = None
    temperature: str | None = None
    max_tokens: str | None = None
    thinking_budget: str | None = None
    context_window: str | None = None
    summarization_model: str | None = None
    max_turns: str | None = None
    cache_retention: str | None = None
    vertex_project_id: str | None = None
    vertex_location: str | None = None
    anthropic_vertex_project_id: str | None = None
    anthropic_vertex_region: str | None = None
    fake_provider_events: str | None = None


@dataclass(frozen=True, slots=True)
class PathsConfig:
    agent_md: str | None = None
    memory_path: str | None = None
    memory_storage_uri: str | None = None
    skills_path: str | None = None
    db_url: str | None = None
    mcp_config: str | None = None
    command_allowlist_config: str | None = None
    permission_config: str | None = None
    workspace_root: str | None = None
    agent_id: str | None = None
    approvals_config: str | None = None
    agent_md_digest: str | None = None
    skills_digest: str | None = None
    mcp_config_digest: str | None = None
    command_allowlist_digest: str | None = None
    permission_config_digest: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    log_level: str | None = None
    port: str | None = None
    gateway_port: str | None = None
    pending_response_timeout_sec: str | None = None
    sse_replay_max: str | None = None
    sse_nested_replay_max: str | None = None
    graceful_shutdown_timeout_sec: str | None = None
    cors_allow_origins: str | None = None
    emission_style: str | None = None
    harness_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ToolsConfig:
    denied_patterns: str | None = None
    resume_thinking_budget: str | None = None
    web_search_backend: str | None = None
    web_search_max_results: str | None = None
    todo_list_enabled: str | None = None
    todo_list_mirror_to_disk: str | None = None
    computer_enabled: str | None = None
    sandbox_enabled: str | None = None
    sandbox_server_url: str | None = None
    sandbox_image: str | None = None
    sandbox_ttl_seconds: str | None = None
    sandbox_shared_filesystem: str | None = None
    scheduler_enabled: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: str | None = None
    backend: str | None = None
    embedding_model: str | None = None


@dataclass(frozen=True, slots=True)
class CurationConfig:
    enabled: str | None = None
    memory_window_lines: str | None = None
    memory_index_cap: str | None = None
    memory_token_threshold: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    """What changed between two snapshots. ``noop`` means digest was unchanged."""

    noop: bool
    changed_env_keys: frozenset[str]
    changed_content: frozenset[str]
    tiers: frozenset[ConfigTier]

    @staticmethod
    def unchanged() -> ConfigDiff:
        return ConfigDiff(
            noop=True,
            changed_env_keys=frozenset(),
            changed_content=frozenset(),
            tiers=frozenset(),
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    revision: int
    digest: str  # yaml + pins + content + overlaid env_values (not yaml+pins alone)
    source_path: Path | None
    loaded_at: float
    model: ModelConfig
    paths: PathsConfig
    gateway: GatewayConfig
    tools: ToolsConfig
    memory: MemoryConfig
    curation: CurationConfig
    realtime: RealtimeConfig
    subagents: Mapping[str, SubagentConfig]
    subagent_settings: SubagentSettings
    env_values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "env_values", MappingProxyType(dict(self.env_values)))
        object.__setattr__(self, "subagents", MappingProxyType(dict(self.subagents)))


def env_field_value(cfg: RuntimeConfig, env_name: str) -> Any:
    """Return the nested ``RuntimeConfig`` field backing an ``ENV_MAP`` key."""
    path = ENV_FIELD_PATHS[env_name]
    current: Any = cfg
    for part in path.split("."):
        current = getattr(current, part)
    return current


class ConfigStore:
    """Process-wide holder of the current ``RuntimeConfig`` pointer."""

    __slots__ = (
        "_current",
        "_config_path",
        "_agent_root",
        "_lock",
        "_source_digest",
        "_overlays",
        "_prepared_disk",
    )

    def __init__(self) -> None:
        self._current: RuntimeConfig | None = None
        self._config_path: Path | None = None
        self._agent_root: Path | None = None
        self._lock = threading.Lock()
        self._source_digest: str | None = None
        self._overlays: dict[str, str] = {}
        self._prepared_disk: dict[int, str] = {}

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

    def prepare_reload(
        self, *, config_path: Path | None = None, agent_root: Path | None = None
    ) -> tuple[RuntimeConfig, ConfigDiff]:
        """Rebuild from disk without swapping the process pointer.

        Call :meth:`commit` after live slices apply successfully so a failed
        apply cannot poison the digest into a no-op that never retries.

        Overlay-only digest changes (layout paths) do not count as a reload:
        comparison uses the last disk-built digest, not the overlay-folded one.
        Layout overlays are re-applied onto the rebuilt snapshot so a later
        admin reload cannot drop ``DB_URL`` / ``AGENT_MD`` / etc.
        """
        with self._lock:
            path = self._config_path if config_path is None else config_path
            root = self._agent_root if agent_root is None else agent_root
            current = self._current
            source_digest = self._source_digest
            overlays = dict(self._overlays)
        built = build_runtime_config(config_path=path, agent_root=root, revision=0)
        if current is not None and source_digest is not None and built.digest == source_digest:
            logger.info(
                "config snapshot prepared %s",
                kv(revision=current.revision, digest=current.digest, noop=True),
            )
            return current, ConfigDiff.unchanged()
        prepared = _apply_overlays(built, overlays, base_digest=built.digest)
        diff = (
            diff_runtime_configs(current, prepared)
            if current is not None
            else ConfigDiff(
                noop=False,
                changed_env_keys=frozenset(prepared.env_values),
                changed_content=frozenset(),
                tiers=frozenset(ENV_TIERS[k] for k in prepared.env_values if k in ENV_TIERS),
            )
        )
        with self._lock:
            self._prepared_disk[id(prepared)] = built.digest
        return prepared, diff

    def commit(
        self,
        cfg: RuntimeConfig,
        *,
        config_path: Path | None = None,
        agent_root: Path | None = None,
    ) -> RuntimeConfig:
        """Atomically publish ``cfg`` as the process current snapshot.

        Revision is minted here under the store lock so two concurrent
        prepare/commit pairs cannot both land as N+1.
        """
        with self._lock:
            path = self._config_path if config_path is None else config_path
            root = self._agent_root if agent_root is None else agent_root
            current = self._current
            revision = 1 if current is None else current.revision + 1
            disk_digest = self._prepared_disk.pop(id(cfg), cfg.digest)
            published = _apply_overlays(
                replace(cfg, revision=revision),
                self._overlays,
                base_digest=disk_digest,
            )
            self._current = published
            self._config_path = path
            self._agent_root = root
            self._source_digest = disk_digest
        logger.info(
            "config snapshot published %s",
            kv(revision=published.revision, digest=published.digest, noop=False),
        )
        return published

    def reload(
        self, *, config_path: Path | None = None, agent_root: Path | None = None
    ) -> tuple[RuntimeConfig, ConfigDiff]:
        """Rebuild from disk. No-op (same object, no revision bump) when digest matches."""
        cfg, diff = self.prepare_reload(config_path=config_path, agent_root=agent_root)
        if not diff.noop:
            cfg = self.commit(cfg, config_path=config_path, agent_root=agent_root)
        return cfg, diff

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
            self._source_digest = cfg.digest

    def _overlay_env(self, updates: Mapping[str, str]) -> None:
        """Merge ``updates`` into ``_current.env_values`` without bumping revision.

        Recomputes digest and typed section views under the same lock as
        ``_set``. No-op when the store is empty or when ``updates`` would not
        change any value. Leaves ``_source_digest`` unchanged so a later
        reload can still no-op when YAML and content files are unchanged.
        Overlay keys persist on the store and are re-applied by
        :meth:`prepare_reload` / :meth:`commit`.
        """
        with self._lock:
            cfg = self._current
            if cfg is None:
                return
            merged = dict(cfg.env_values)
            changed = False
            for key, value in updates.items():
                self._overlays[key] = value
                if merged.get(key) != value:
                    merged[key] = value
                    changed = True
            if not changed:
                return
            base = self._source_digest or cfg.digest
            self._current = _snapshot_with_env(cfg, merged, base_digest=base)

    def _reset(self) -> None:
        with self._lock:
            self._current = None
            self._config_path = None
            self._agent_root = None
            self._source_digest = None
            self._overlays = {}
            self._prepared_disk = {}


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


def env_value_or_current(cfg: RuntimeConfig | None, key: str, default: str = "") -> str:
    """Read from a pinned snapshot, else the process store (else ``os.environ``).

    ``env_value(None, …)`` skips the store and reads ``os.environ`` directly.
    Call sites that used to go through ``current_env`` when unpinned should use
    this helper so a live ``ConfigStore`` snapshot is not bypassed.
    """
    if cfg is not None:
        return env_value(cfg, key, default)
    return current_env(key, default)


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
    ``current_env`` and the typed section views after bootstrap. No-op when
    the store is empty.

    This completes the same revision rather than a YAML reload. Digest is
    recomputed over the new ``env_values`` so a cache keyed on digest cannot
    serve stale layout paths. Serialized under the store lock against ``_set``.
    """
    get_config_store()._overlay_env(updates)


# App-owned spawn keys the admin reload endpoint may patch. Pins *and* process
# env are updated so subprocess transport (MCP / subagent workers) inherits them.
RELOAD_PIN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "MONKEYBOT_COMPUTER_TOOLS",
    }
)


def pinned_env_names() -> frozenset[str]:
    """ENV_MAP (and extra) keys captured as pins on first build."""
    if _PINNED_ENV is None:
        return frozenset()
    return frozenset(_PINNED_ENV)


def apply_reload_env_patch(patch: Mapping[str, str]) -> dict[str, str]:
    """Update allowlisted app-owned pins. Returns the keys actually applied.

    Captures operator env pins first so a patch cannot initialize ``_PINNED_ENV``
    to ``{}`` and permanently skip :func:`_capture_pins`.
    """
    pinned = _capture_pins()
    applied: dict[str, str] = {}
    for key, value in patch.items():
        if key not in RELOAD_PIN_ALLOWLIST or not isinstance(value, str):
            continue
        pinned[key] = value
        os.environ[key] = value
        applied[key] = value
    logger.info("reload env patch applied %s", kv(keys=",".join(sorted(applied))))
    return applied


def capture_reload_pins(keys: Iterable[str]) -> dict[str, tuple[str | None, str | None]]:
    """Snapshot pin *and* ``os.environ`` values for ``keys`` before an env patch.

    The two stores are not always in sync: an operator env var can be present
    in ``os.environ`` while absent from ``_PINNED_ENV``. Pair with
    :func:`restore_reload_pins` so rollback restores each independently
    instead of ``pop``-ing a real env var that was never a pin.
    """
    current = _PINNED_ENV or {}
    return {key: (current.get(key), os.environ.get(key)) for key in keys}


def restore_reload_pins(prev: Mapping[str, tuple[str | None, str | None]]) -> None:
    """Undo a previously applied :func:`apply_reload_env_patch` on failure."""
    global _PINNED_ENV
    if _PINNED_ENV is None:
        _PINNED_ENV = {}
    for key, (pin_value, environ_value) in prev.items():
        if pin_value is None:
            _PINNED_ENV.pop(key, None)
        else:
            _PINNED_ENV[key] = pin_value
        if environ_value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = environ_value
    logger.info("reload env patch rolled back %s", kv(keys=",".join(sorted(prev))))


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
    check_yaml_only_model_env(merged)
    anchor = agent_root or resolve_agent_root(config_path=source_path)
    env_values = _effective_env(_flatten_config(merged), pinned, anchor)
    content = _content_digests(env_values, anchor)
    digest = _compute_digest(merged=merged, pinned=pinned, content=content)
    subagents, subagent_settings = _parse_subagents(merged)
    return RuntimeConfig(
        revision=revision,
        digest=digest,
        source_path=source_path,
        loaded_at=time.time(),
        model=_model_from_env(env_values),
        paths=_paths_from_env(env_values, content),
        gateway=_gateway_from_env(env_values),
        tools=_tools_from_env(env_values),
        memory=_memory_from_env(env_values),
        curation=_curation_from_env(env_values),
        realtime=realtime_config_from_doc(merged, env_values),
        subagents=subagents,
        subagent_settings=subagent_settings,
        env_values=env_values,
    )


def diff_runtime_configs(old: RuntimeConfig, new: RuntimeConfig) -> ConfigDiff:
    if old.digest == new.digest:
        return ConfigDiff.unchanged()
    changed_env: set[str] = set()
    for key in set(old.env_values) | set(new.env_values):
        if old.env_values.get(key) != new.env_values.get(key):
            changed_env.add(key)
    changed_content: set[str] = set()
    old_content = _paths_content(old.paths)
    new_content = _paths_content(new.paths)
    for name, digest in new_content.items():
        if old_content.get(name) != digest:
            changed_content.add(name)
            env_key = CONTENT_DIGEST_TIERS.get(name)
            if env_key is not None:
                changed_env.add(env_key)
    if dict(old.subagents) != dict(new.subagents):
        changed_content.add("subagents")
        changed_env.add(SUBAGENTS_DIFF_KEY)
    if old.subagent_settings != new.subagent_settings:
        changed_content.add("subagent_settings")
        changed_env.add(SUBAGENTS_DIFF_KEY)
    tiers = {ENV_TIERS[k] for k in changed_env if k in ENV_TIERS}
    if SUBAGENTS_DIFF_KEY in changed_env:
        tiers.add(ConfigTier.REBUILD)
    return ConfigDiff(
        noop=False,
        changed_env_keys=frozenset(changed_env),
        changed_content=frozenset(changed_content),
        tiers=frozenset(tiers),
    )


def _capture_pins() -> dict[str, str]:
    global _PINNED_ENV
    if _PINNED_ENV is not None:
        return _PINNED_ENV
    pinned: dict[str, str] = {}
    for env_name in ENV_MAP.values():
        if env_name in YAML_ONLY_ENV_KEYS:
            continue
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


_HASH_CHUNK = 65536
_SKILLS_HASH_MAX_FILES = 2048


def _update_digest_from_file(digest: Any, path: Path) -> None:
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)


def _hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    _update_digest_from_file(digest, path)
    return digest.hexdigest()


def _hash_tree(path: Path) -> str | None:
    """Digest a skills tree from relative path + mtime + size, not file bytes.

    ``SKILLS_PATH`` is user-pointed and unbounded; reading every file on each
    apply/reload would walk vendored deps and binaries. Metadata is enough to
    notice edits. Caps the file count so a huge tree cannot stall startup.
    """
    if path.is_file():
        return _hash_file(path)
    if not path.is_dir():
        return None
    files: list[Path] = []
    truncated = False
    for child in path.rglob("*"):
        try:
            is_file = child.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        files.append(child)
        if len(files) >= _SKILLS_HASH_MAX_FILES:
            truncated = True
            break
    files.sort(key=lambda p: p.as_posix())
    digest = hashlib.sha256()
    for child in files:
        try:
            st = child.stat()
            rel = child.relative_to(path).as_posix().encode()
        except OSError:
            continue
        digest.update(rel)
        digest.update(b"\0")
        digest.update(f"{st.st_mtime_ns}:{st.st_size}".encode())
        digest.update(b"\0")
    if truncated:
        digest.update(b"truncated\0")
        logger.warning(
            "skills tree hash capped at %s files: %s",
            _SKILLS_HASH_MAX_FILES,
            path,
        )
    return digest.hexdigest()


def _resolved_content_path(env_values: Mapping[str, str], env_key: str, anchor: Path) -> Path:
    raw = env_values.get(env_key) or _DEFAULT_CONTENT_PATHS[env_key]
    return resolve_agent_path(raw, anchor)


def _content_digests(env_values: Mapping[str, str], anchor: Path) -> dict[str, str | None]:
    return {
        "agent_md": _hash_file(_resolved_content_path(env_values, "AGENT_MD", anchor)),
        "skills": _hash_tree(_resolved_content_path(env_values, "SKILLS_PATH", anchor)),
        "mcp_config": _hash_file(_resolved_content_path(env_values, "MCP_CONFIG", anchor)),
        "command_allowlist": _hash_file(
            _resolved_content_path(env_values, "COMMAND_ALLOWLIST_CONFIG", anchor)
        ),
        "permission_config": _hash_file(
            _resolved_content_path(env_values, "PERMISSION_CONFIG", anchor)
        ),
    }


def _paths_content(paths: PathsConfig) -> dict[str, str | None]:
    return {
        "agent_md": paths.agent_md_digest,
        "skills": paths.skills_digest,
        "mcp_config": paths.mcp_config_digest,
        "command_allowlist": paths.command_allowlist_digest,
        "permission_config": paths.permission_config_digest,
    }


def _compute_digest(
    *,
    merged: Mapping[str, Any],
    pinned: Mapping[str, str],
    content: Mapping[str, str | None],
) -> str:
    payload = {
        "yaml": merged,
        "pinned": {k: pinned[k] for k in sorted(pinned)},
        "content": content,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _digest_with_env(base_digest: str, env_values: Mapping[str, str]) -> str:
    """Fold ``env_values`` into a YAML+pins+content digest so overlay invalidates caches."""
    payload = {"base": base_digest, "env": {k: env_values[k] for k in sorted(env_values)}}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _snapshot_with_env(
    cfg: RuntimeConfig, merged: dict[str, str], *, base_digest: str
) -> RuntimeConfig:
    """Copy ``cfg`` with ``merged`` env_values, a folded digest, and rebuilt typed views."""
    return replace(
        cfg,
        env_values=merged,
        digest=_digest_with_env(base_digest, merged),
        model=_model_from_env(merged),
        paths=_paths_from_env(merged, _paths_content(cfg.paths)),
        gateway=_gateway_from_env(merged),
        tools=_tools_from_env(merged),
        memory=_memory_from_env(merged),
        curation=_curation_from_env(merged),
    )


def _apply_overlays(
    cfg: RuntimeConfig, overlays: Mapping[str, str], *, base_digest: str
) -> RuntimeConfig:
    if not overlays:
        return cfg
    merged = dict(cfg.env_values)
    changed = False
    for key, value in overlays.items():
        if merged.get(key) != value:
            merged[key] = value
            changed = True
    if not changed:
        return cfg
    return _snapshot_with_env(cfg, merged, base_digest=base_digest)


def _model_from_env(env: Mapping[str, str]) -> ModelConfig:
    return ModelConfig(
        provider=env.get("MODEL_PROVIDER"),
        name=env.get("MODEL_NAME"),
        temperature=env.get("MODEL_TEMPERATURE"),
        max_tokens=env.get("MODEL_MAX_TOKENS"),
        thinking_budget=env.get("MODEL_THINKING_BUDGET"),
        context_window=env.get("MODEL_CONTEXT_WINDOW"),
        summarization_model=env.get("CONTEXT_SUMMARIZATION_MODEL"),
        max_turns=env.get("MAX_TURNS"),
        cache_retention=env.get("MODEL_CACHE_RETENTION"),
        vertex_project_id=env.get("VERTEX_AI_PROJECT_ID"),
        vertex_location=env.get("VERTEX_AI_LOCATION"),
        anthropic_vertex_project_id=env.get("ANTHROPIC_VERTEX_PROJECT_ID"),
        anthropic_vertex_region=env.get("ANTHROPIC_VERTEX_REGION"),
        fake_provider_events=env.get("MONKEYBOT_FAKE_PROVIDER_EVENTS"),
    )


def _paths_from_env(env: Mapping[str, str], content: Mapping[str, str | None]) -> PathsConfig:
    return PathsConfig(
        agent_md=env.get("AGENT_MD"),
        memory_path=env.get("MEMORY_PATH"),
        memory_storage_uri=env.get("MEMORY_STORAGE_URI"),
        skills_path=env.get("SKILLS_PATH"),
        db_url=env.get("DB_URL"),
        mcp_config=env.get("MCP_CONFIG"),
        command_allowlist_config=env.get("COMMAND_ALLOWLIST_CONFIG"),
        permission_config=env.get("PERMISSION_CONFIG"),
        workspace_root=env.get("MONKEYBOT_WORKSPACE_ROOT"),
        agent_id=env.get("MONKEYBOT_AGENT_ID"),
        approvals_config=env.get("MONKEYBOT_APPROVALS_CONFIG"),
        agent_md_digest=content.get("agent_md"),
        skills_digest=content.get("skills"),
        mcp_config_digest=content.get("mcp_config"),
        command_allowlist_digest=content.get("command_allowlist"),
        permission_config_digest=content.get("permission_config"),
    )


def _gateway_from_env(env: Mapping[str, str]) -> GatewayConfig:
    return GatewayConfig(
        log_level=env.get("LOG_LEVEL"),
        port=env.get("PORT"),
        gateway_port=env.get("GATEWAY_PORT"),
        pending_response_timeout_sec=env.get("PENDING_RESPONSE_TIMEOUT_SEC"),
        sse_replay_max=env.get("SSE_REPLAY_MAX"),
        sse_nested_replay_max=env.get("SSE_NESTED_REPLAY_MAX"),
        graceful_shutdown_timeout_sec=env.get("GRACEFUL_SHUTDOWN_TIMEOUT_SEC"),
        cors_allow_origins=env.get("MONKEYBOT_CORS_ALLOW_ORIGINS"),
        emission_style=env.get("MONKEYBOT_EMISSION_STYLE"),
        harness_mode=env.get("MONKEYBOT_HARNESS_MODE"),
    )


def _tools_from_env(env: Mapping[str, str]) -> ToolsConfig:
    return ToolsConfig(
        denied_patterns=env.get("MONKEYBOT_TOOL_DENIED_PATTERNS"),
        resume_thinking_budget=env.get("MONKEYBOT_RESUME_THINKING_BUDGET"),
        web_search_backend=env.get("WEB_SEARCH_BACKEND"),
        web_search_max_results=env.get("WEB_SEARCH_MAX_RESULTS"),
        todo_list_enabled=env.get("MONKEYBOT_TODO_LIST_ENABLED"),
        todo_list_mirror_to_disk=env.get("MONKEYBOT_TODO_LIST_MIRROR_TO_DISK"),
        computer_enabled=env.get("MONKEYBOT_COMPUTER_TOOLS"),
        sandbox_enabled=env.get("SANDBOX_ENABLED"),
        sandbox_server_url=env.get("SANDBOX_SERVER_URL"),
        sandbox_image=env.get("SANDBOX_IMAGE"),
        sandbox_ttl_seconds=env.get("SANDBOX_TTL_SECONDS"),
        sandbox_shared_filesystem=env.get("SANDBOX_SHARED_FILESYSTEM"),
        scheduler_enabled=env.get("MONKEYBOT_SCHEDULER_ENABLED"),
    )


def _memory_from_env(env: Mapping[str, str]) -> MemoryConfig:
    return MemoryConfig(
        enabled=env.get("MONKEYBOT_MEMORY_HOOK_ENABLED"),
        backend=env.get("MEMPALACE_BACKEND"),
        embedding_model=env.get("MEMPALACE_EMBEDDING_MODEL"),
    )


def _curation_from_env(env: Mapping[str, str]) -> CurationConfig:
    return CurationConfig(
        enabled=env.get("CONTEXT_CURATION_ENABLED"),
        memory_window_lines=env.get("CONTEXT_CURATION_MEMORY_WINDOW_LINES"),
        memory_index_cap=env.get("MEMORY_INDEX_CAP"),
        memory_token_threshold=env.get("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD"),
    )


def _parse_subagents(
    doc: Mapping[str, Any],
) -> tuple[dict[str, SubagentConfig], SubagentSettings]:
    """Parse ``subagents:`` for the snapshot.

    Invalid or legacy shapes (bare list, bad types) warn and fall back to
    defaults so a malformed section cannot abort ``apply_monkeybot_runtime_env``
    / ``bootstrap_agent_layout``. Duplicate persona names still raise
    :class:`ConfigError` so bootstrap and ``prepare_reload`` fail closed —
    last-write-wins would silently drop a persona. ``get_subagent_settings``
    still raises when the task tool actually asks for subagents.
    """
    try:
        section = _subagents_section(dict(doc))
        settings = _subagent_settings_from_section(section)
    except ConfigError as exc:
        logger.warning("Ignoring invalid subagents section during snapshot load: %s", exc)
        return {}, SubagentSettings()
    personas = _persona_registry(_parse_subagent_entries(section.get("personas")))
    return personas, settings


__all__ = [
    "ConfigDiff",
    "ConfigStore",
    "ConfigTier",
    "CurationConfig",
    "ENV_FIELD_PATHS",
    "GatewayConfig",
    "MemoryConfig",
    "ModelConfig",
    "PathsConfig",
    "RuntimeConfig",
    "ToolsConfig",
    "build_runtime_config",
    "diff_runtime_configs",
    "env_field_value",
    "get_config_store",
    "context_window_tokens",
    "current_env",
    "current_env_flag",
    "current_env_or_none",
    "env_flag",
    "env_value",
    "env_value_or_current",
    "load_into_store",
    "overlay_env_values",
    "pinned_env_names",
    "apply_reload_env_patch",
    "capture_reload_pins",
    "restore_reload_pins",
    "reset_snapshot_state_for_tests",
]
