"""Resolve monkeybot.yaml for CLI commands."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from monkeybot.core.config.yaml_loader import (
    load_monkeybot_yaml_dict,
    resolve_monkeybot_config_path,
)


def resolve_agent_root(*, cwd: Path | None = None, config_path: Path | None = None) -> Path:
    """Return the agent project root (directory containing monkeybot_config/ or .env)."""
    if cwd is not None:
        return cwd.expanduser().resolve()
    if config_path is not None:
        cp = config_path.expanduser().resolve()
        if cp.parent.name == "monkeybot_config":
            return cp.parent.parent
        return cp.parent
    return Path.cwd()


def load_agent_dotenv(*, cwd: Path | None = None, config_path: Path | None = None) -> Path | None:
    """Load ``.env`` from the agent root (``--cwd`` or parent of ``monkeybot_config``).

    ``uv run`` sets the process cwd to the CLI package, so bare ``load_dotenv()`` misses
    agent secrets. Existing environment variables are not overridden.
    """
    root = resolve_agent_root(cwd=cwd, config_path=config_path)
    env_file = root / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)
        return env_file
    fallback = Path.cwd() / ".env"
    if fallback.is_file() and fallback != env_file:
        load_dotenv(fallback, override=False)
        return fallback
    return None


def resolve_config(explicit: str | None, *, cwd: Path | None = None) -> Path | None:
    """Resolve config path from --config flag or defaults."""
    if explicit:
        p = Path(explicit).expanduser()
        return p.resolve() if p.is_file() else None
    resolved_cwd = cwd.expanduser().resolve() if cwd is not None else None
    return resolve_monkeybot_config_path(cwd=resolved_cwd)


def load_config_doc(config_path: str | Path | None = None) -> tuple[Path | None, dict]:
    path, doc = load_monkeybot_yaml_dict(config_path)
    return path, doc if isinstance(doc, dict) else {}
