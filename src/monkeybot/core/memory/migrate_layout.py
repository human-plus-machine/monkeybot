"""One-shot migration: agent ``data/memory`` → ``memory`` (layout rename).

Runs for every local agent under ``~/.monkeybot/agents/`` (plus the current
agent root) so Mac app / CLI / gateway startups all stay compatible after the
default path change.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LEGACY_REL = Path("data") / "memory"
_CANONICAL_REL = Path("memory")

_URI_REPLACEMENTS = (
    ("local://./data/memory", "local://./memory"),
    ("local://data/memory", "local://./memory"),
)

_ALLOWLIST_REPLACEMENTS = (
    ("./data/memory/", "../memory/"),
    ("./data/memory", "../memory"),
)


def _monkeybot_home() -> Path:
    raw = os.environ.get("MONKEYBOT_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".monkeybot").resolve()


def discover_local_agent_roots(*, include: Path | None = None) -> list[Path]:
    """Return agent roots that look like monkeybot agents (have monkeybot_config/)."""
    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if resolved in seen:
            return
        if not (resolved / "monkeybot_config").is_dir():
            return
        seen.add(resolved)
        roots.append(resolved)

    agents_dir = _monkeybot_home() / "agents"
    if agents_dir.is_dir():
        try:
            children = sorted(agents_dir.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("Cannot list %s for memory layout migrate: %r", agents_dir, exc)
            children = []
        for child in children:
            if child.is_dir():
                _add(child)

    if include is not None:
        _add(include)

    return roots


def _rewrite_text_file(path: Path, replacements: tuple[tuple[str, str], ...]) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read %s during memory layout migrate: %r", path, exc)
        return False
    new = text
    for old, repl in replacements:
        new = new.replace(old, repl)
    if new == text:
        return False
    try:
        path.write_text(new, encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot write %s during memory layout migrate: %r", path, exc)
        return False
    return True


def _move_legacy_memory_dir(agent_root: Path) -> str | None:
    """Move ``data/memory`` → ``memory``. Returns action label or None."""
    legacy = (agent_root / _LEGACY_REL).resolve()
    dest = (agent_root / _CANONICAL_REL).resolve()
    if not legacy.is_dir():
        return None
    if legacy == dest:
        return None
    if dest.exists():
        logger.warning(
            "Skipping memory dir move for %s: both %s and %s exist — "
            "keeping canonical memory/; remove data/memory manually if unused",
            agent_root.name,
            legacy,
            dest,
        )
        return "skipped_both_exist"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(dest))
    except OSError as exc:
        logger.warning(
            "Failed to move %s → %s for agent %s: %r",
            legacy,
            dest,
            agent_root.name,
            exc,
        )
        return "move_failed"
    logger.info("Migrated memory layout for %s: %s → %s", agent_root.name, legacy, dest)
    return "moved"


def migrate_agent_memory_layout(agent_root: Path) -> dict[str, Any]:
    """Migrate one agent root. Idempotent."""
    root = agent_root.expanduser().resolve()
    result: dict[str, Any] = {
        "agent_root": str(root),
        "moved": False,
        "yaml_updated": False,
        "allowlist_updated": False,
        "example_yaml_updated": False,
        "action": "noop",
    }

    move_action = _move_legacy_memory_dir(root)
    if move_action == "moved":
        result["moved"] = True
        result["action"] = "migrated"
    elif move_action:
        result["action"] = move_action

    config_dir = root / "monkeybot_config"
    yaml_path = config_dir / "monkeybot.yaml"
    if _rewrite_text_file(yaml_path, _URI_REPLACEMENTS):
        result["yaml_updated"] = True
        result["action"] = "migrated" if result["action"] in ("noop", "migrated") else result["action"]
        logger.info("Updated memory_storage_uri in %s", yaml_path)

    example = config_dir / "monkeybot.example.yaml"
    if _rewrite_text_file(example, _URI_REPLACEMENTS):
        result["example_yaml_updated"] = True

    allowlist = config_dir / "command_allowlist.yaml"
    if _rewrite_text_file(allowlist, _ALLOWLIST_REPLACEMENTS):
        result["allowlist_updated"] = True
        if result["action"] == "noop":
            result["action"] = "migrated"
        logger.info("Updated memory allowlist paths in %s", allowlist)

    return result


def migrate_all_local_agent_memory_layouts(
    *, include: Path | None = None
) -> list[dict[str, Any]]:
    """Migrate every discovered local agent (plus optional ``include`` root)."""
    results: list[dict[str, Any]] = []
    for root in discover_local_agent_roots(include=include):
        try:
            results.append(migrate_agent_memory_layout(root))
        except Exception as exc:  # noqa: BLE001 — never block bootstrap
            logger.warning("Memory layout migrate failed for %s: %r", root, exc)
            results.append(
                {
                    "agent_root": str(root),
                    "moved": False,
                    "yaml_updated": False,
                    "allowlist_updated": False,
                    "example_yaml_updated": False,
                    "action": "error",
                    "error": repr(exc),
                }
            )
    migrated = [r for r in results if r.get("action") == "migrated" or r.get("moved")]
    if migrated:
        logger.info(
            "Memory layout migrate: updated %d agent(s): %s",
            len(migrated),
            ", ".join(Path(r["agent_root"]).name for r in migrated),
        )
    return results


__all__ = [
    "discover_local_agent_roots",
    "migrate_agent_memory_layout",
    "migrate_all_local_agent_memory_layouts",
]
