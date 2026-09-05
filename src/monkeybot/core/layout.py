"""Canonical agent-root layout discovery and path resolution.

Every runtime entry point resolves relative configuration from the agent root:
the directory that contains ``monkeybot_config/``.  This deliberately avoids
using the process working directory, which may be a CLI package, a workspace,
or an arbitrary service-manager directory.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from monkeybot.core.memory.uri import (
    DEFAULT_LOCAL_MEMORY_RELPATH,
    object_store_memory_scheme,
)

_log = logging.getLogger(__name__)


def _config_root(path: Path) -> Path:
    """Return the agent root for a config file, including explicit configs."""
    resolved = path.expanduser().resolve()
    return resolved.parent.parent if resolved.parent.name == "monkeybot_config" else resolved.parent


def resolve_agent_root(*, cwd: Path | None = None, config_path: Path | None = None) -> Path:
    """Find the nearest parent directory containing ``monkeybot_config``.

    An explicit config path has priority.  With no config (library embedding,
    for example), the supplied cwd is retained as a conservative fallback; it
    never triggers the old ``cwd/workspace`` layout guess.
    """
    if config_path is not None:
        return _config_root(config_path)

    raw_config = os.environ.get("MONKEYBOT_CONFIG", "").strip()
    if raw_config:
        candidate = Path(raw_config).expanduser()
        if candidate.is_file():
            return _config_root(candidate)

    base = (cwd or Path.cwd()).expanduser().resolve()
    for candidate in (base, *base.parents):
        if (candidate / "monkeybot_config").is_dir():
            return candidate

    legacy_root = os.environ.get("MONKEYBOT_AGENT_ROOT", "").strip()
    if legacy_root:
        return Path(legacy_root).expanduser().resolve()
    return base


def resolve_config_path(*, agent_root: Path, explicit: str | Path | None = None) -> Path | None:
    """Resolve an explicit or conventional config path without cwd coupling."""
    raw = explicit if explicit is not None else os.environ.get("MONKEYBOT_CONFIG", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = agent_root / path
        return path.resolve() if path.is_file() else None
    default = agent_root / "monkeybot_config" / "monkeybot.yaml"
    return default.resolve() if default.is_file() else None


def resolve_agent_path(raw: str | Path, agent_root: Path) -> Path:
    """Resolve one configured filesystem path against ``agent_root``."""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (agent_root / path).resolve()


def resolve_workspace_root_override() -> Path | None:
    """Absolute host path that remaps the agent workspace for one process.

    Used by Monkeybot Mac workspace sessions so attachments / file tools land
    under ``~/.monkeybot/workspaces/<id>/memory`` instead of the agent's
    ``paths.workspace_root``. Relative values are ignored.
    """
    raw = os.environ.get("MONKEYBOT_WORKSPACE_ROOT_OVERRIDE", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        _log.warning(
            "Ignoring relative MONKEYBOT_WORKSPACE_ROOT_OVERRIDE=%r (must be absolute)",
            raw,
        )
        return None
    resolved = path.resolve()
    _log.info(
        "Workspace root remapped via MONKEYBOT_WORKSPACE_ROOT_OVERRIDE to %s",
        resolved,
    )
    return resolved


def resolve_workspace_root(*, agent_root: Path, config_path: Path | None = None) -> Path:
    """Resolve workspace root from yaml, with an optional absolute process override.

    ``MONKEYBOT_WORKSPACE_ROOT_OVERRIDE`` (absolute) wins when set — used by the
    Mac app to remap a gateway onto a workspace shared-memory directory.
    Otherwise ``paths.workspace_root`` in monkeybot.yaml is the source of truth
    (plain ``MONKEYBOT_WORKSPACE_ROOT`` / legacy ``WORKSPACE_ROOT`` do not win).
    When the yaml key is absent, falls back to ``<agent_root>/workspace``.
    """
    if override := resolve_workspace_root_override():
        return override

    workspace_raw = "workspace"
    if config_path is not None:
        from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

        _, doc = load_monkeybot_yaml_dict(config_path)
        paths = doc.get("paths") if isinstance(doc, dict) else None
        if isinstance(paths, dict):
            wr = paths.get("workspace_root")
            if isinstance(wr, str) and wr.strip():
                workspace_raw = wr.strip()
    return resolve_agent_path(workspace_raw, agent_root)


def resolve_sqlite_url(raw: str, agent_root: Path) -> str:
    """Anchor a relative ``sqlite:///`` URL at the agent root."""
    prefix = "sqlite:///"
    if not raw.lower().startswith(prefix):
        return raw
    remainder = raw[len(prefix) :]
    if not remainder or remainder == ":memory:":
        return raw
    path = Path(remainder)
    if path.is_absolute():
        return raw
    return f"{prefix}{resolve_agent_path(path, agent_root)}"


def resolve_memory_storage_uri(raw: str, agent_root: Path) -> str:
    """Anchor local memory URIs at the agent root.

    Object-store schemes are not supported. Log once and fall back to
    ``local://`` under the agent root so existing ``gcs://`` / ``s3://``
    deployments still boot.
    """
    value = raw.strip()
    remote = object_store_memory_scheme(value)
    if remote:
        fallback = f"local://{resolve_agent_path(DEFAULT_LOCAL_MEMORY_RELPATH, agent_root)}"
        _log.warning(
            "unsupported memory URI %r (%s); falling back to %s",
            value,
            remote,
            fallback,
        )
        return fallback
    scheme, _, rest = value.partition("://")
    local_path = rest if rest and scheme.lower() in {"local", "file"} else value
    return f"local://{resolve_agent_path(local_path or DEFAULT_LOCAL_MEMORY_RELPATH, agent_root)}"


@dataclass(frozen=True)
class AgentLayout:
    """Resolved canonical locations for a single agent process."""

    agent_root: Path
    config_path: Path | None
    config_dir: Path
    workspace_root: Path
    skills_path: Path
    artifacts_path: Path | None
    data_root: Path
    agent_md_path: Path
    mcp_config_path: Path
    command_allowlist_path: Path
    permission_config_path: Path
    approvals_path: Path
    db_url: str
    memory_storage_uri: str
    agent_id: str

    @classmethod
    def from_environment(
        cls, *, agent_root: Path | None = None, config_path: Path | None = None
    ) -> "AgentLayout":
        root = (agent_root or resolve_agent_root(config_path=config_path)).resolve()
        cfg = config_path or resolve_config_path(agent_root=root)

        def path_env(name: str, default: str) -> Path:
            return resolve_agent_path(os.environ.get(name, default), root)

        workspace = resolve_workspace_root(agent_root=root, config_path=cfg)
        data = root / "data"

        # artifacts_path is opt-in only (unlike skills_path, which always has a
        # default) — it's a *writable* extra mount, and there's no single safe
        # default location to derive: for a plain agent it'd be a sibling of
        # agent_root, but for a customized `paths.workspace_root` (e.g.
        # /code/myproject) that same derivation would land outside both the
        # project and agent_root entirely. The caller that actually knows the
        # real mount point (e.g. the Mac app, which symlinks
        # `<workspace_root>/artifacts` -> a sibling directory) must set
        # ARTIFACTS_PATH explicitly; absent that, no extra root is granted and
        # `artifacts/...` paths are validated (and rejected) like any other.
        artifacts_env = os.environ.get("ARTIFACTS_PATH")
        artifacts_path = resolve_agent_path(artifacts_env, root) if artifacts_env else None

        return cls(
            agent_root=root,
            config_path=cfg,
            config_dir=root / "monkeybot_config",
            workspace_root=workspace,
            skills_path=path_env("SKILLS_PATH", "skills"),
            artifacts_path=artifacts_path,
            data_root=data.resolve(),
            agent_md_path=path_env("AGENT_MD", "monkeybot_config/AGENT.md"),
            mcp_config_path=path_env("MCP_CONFIG", "monkeybot_config/mcp.json"),
            command_allowlist_path=path_env(
                "COMMAND_ALLOWLIST_CONFIG", "monkeybot_config/command_allowlist.yaml"
            ),
            permission_config_path=path_env(
                "PERMISSION_CONFIG", "monkeybot_config/permissions.yaml"
            ),
            approvals_path=path_env(
                "MONKEYBOT_APPROVALS_CONFIG", "monkeybot_config/approvals.json"
            ),
            db_url=resolve_sqlite_url(
                os.environ.get("DB_URL", "sqlite:///data/monkeybot.db"), root
            ),
            memory_storage_uri=resolve_memory_storage_uri(
                os.environ.get(
                    "MEMORY_STORAGE_URI",
                    os.environ.get("MEMORY_PATH", "memory/mempalace"),
                ),
                root,
            ),
            agent_id=os.environ.get("MONKEYBOT_AGENT_ID", "").strip() or str(root),
        )

    def export_environment(self) -> None:
        """Export absolute runtime paths for child processes and legacy consumers."""
        values = {
            "MONKEYBOT_AGENT_ROOT": str(self.agent_root),
            "MONKEYBOT_AGENT_ID": self.agent_id,
            "MONKEYBOT_WORKSPACE_ROOT": str(self.workspace_root),
            "SKILLS_PATH": str(self.skills_path),
            "AGENT_MD": str(self.agent_md_path),
            "MCP_CONFIG": str(self.mcp_config_path),
            "COMMAND_ALLOWLIST_CONFIG": str(self.command_allowlist_path),
            "PERMISSION_CONFIG": str(self.permission_config_path),
            "MONKEYBOT_APPROVALS_CONFIG": str(self.approvals_path),
            "DB_URL": self.db_url,
            "MEMORY_STORAGE_URI": self.memory_storage_uri,
            "MONKEYBOT_PYTHON": sys.executable,
        }
        if self.artifacts_path is not None:
            values["ARTIFACTS_PATH"] = str(self.artifacts_path)
        from monkeybot.core.memory.config import memory_enabled_from_config

        if memory_enabled_from_config():
            values["MEMPALACE_PALACE_PATH"] = self.memory_storage_uri.removeprefix("local://")
            values["MEMPALACE_BACKEND"] = os.environ.get("MEMPALACE_BACKEND", "chroma")
        for key, value in values.items():
            os.environ[key] = value
        from monkeybot.core.config.snapshot import overlay_env_values

        overlay_env_values(values)


def bootstrap_agent_layout(
    *, cwd: Path | None = None, config_path: Path | None = None
) -> AgentLayout:
    """Load root ``.env``, apply YAML defaults, then export the resolved layout.

    Callers that already resolved a config (notably CLI commands with
    ``--config``/``--cwd``) pass it through so the bootstrap cannot fall back
    to the launcher process directory.
    """
    root = resolve_agent_root(cwd=cwd, config_path=config_path)

    # One-shot: data/memory → memory for this agent root (before env/URI resolution).
    # Set MONKEYBOT_MIGRATE_ALL_AGENTS=1 to also migrate every agent under ~/.monkeybot/agents/.
    try:
        from monkeybot.core.memory.migrate_layout import (
            migrate_all_local_agent_memory_layouts,
        )

        migrate_all_local_agent_memory_layouts(include=root)
    except Exception as exc:  # noqa: BLE001 — layout must still boot
        _log.warning("Memory layout migrate skipped: %r", exc)

    env_file = root / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)

    resolved_config = config_path or resolve_config_path(agent_root=root)
    from monkeybot.core.config.runtime_env import apply_monkeybot_runtime_env

    apply_monkeybot_runtime_env(config_path=resolved_config, agent_root=root)
    layout = AgentLayout.from_environment(agent_root=root, config_path=resolved_config)
    layout.export_environment()
    return layout


__all__ = [
    "AgentLayout",
    "bootstrap_agent_layout",
    "resolve_agent_path",
    "resolve_agent_root",
    "resolve_config_path",
    "resolve_memory_storage_uri",
    "resolve_sqlite_url",
    "resolve_workspace_root",
    "resolve_workspace_root_override",
]
