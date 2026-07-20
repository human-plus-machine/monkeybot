"""One-shot migration: agent ``data/memory`` → ``memory`` (layout rename).

By default bootstrap migrates only the agent that is starting. Pass
``migrate_all=True`` (or set ``MONKEYBOT_MIGRATE_ALL_AGENTS=1``) to touch every
local agent under ``~/.monkeybot/agents/``.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LEGACY_REL = Path("data") / "memory"
_CANONICAL_REL = Path("memory")
_LOCK_NAME = ".memory-layout-migrate.lock"

_URI_REPLACEMENTS = (
    ("local://./data/memory", "local://./memory"),
    ("local://data/memory", "local://./memory"),
    ("local:///tmp/monkeybot-data/memory", "local:///tmp/monkeybot-memory"),
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


def _is_scaffold_only(dest: Path) -> bool:
    """True when ``dest`` only has ensure_memory scaffold (no real notes)."""
    if not dest.is_dir():
        return False
    try:
        for path in dest.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(dest).as_posix()
            if path.name == ".gitkeep":
                continue
            if rel == "INDEX.md":
                continue
            # Any other file (a real note under typed folders, raw logs, etc.)
            return False
        return True
    except OSError:
        return False


def _merge_legacy_into_dest(legacy: Path, dest: Path) -> None:
    """Copy missing files from legacy into dest, then remove legacy."""
    for src in legacy.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(legacy)
        target = dest / rel
        if target.exists():
            # Prefer legacy note content over empty scaffold INDEX when both exist
            # only if dest file is a scaffold placeholder INDEX with no entries.
            if rel.as_posix() == "INDEX.md":
                try:
                    dest_text = target.read_text(encoding="utf-8")
                    src_text = src.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "[[" not in dest_text and "[[" in src_text:
                    target.write_text(src_text, encoding="utf-8")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    shutil.rmtree(legacy, ignore_errors=True)


def _acquire_migrate_lock(agent_root: Path) -> Path | None:
    """Exclusive lock file; returns lock path or None if another migrator holds it."""
    lock_path = agent_root / _LOCK_NAME
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Stale lock older than 5 minutes → steal; else skip.
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return None
        if age < 300:
            return None
        try:
            lock_path.unlink(missing_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
    except OSError as exc:
        logger.warning("Cannot create migrate lock for %s: %r", agent_root, exc)
        return None
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    finally:
        os.close(fd)
    return lock_path


def _release_migrate_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _move_legacy_memory_dir(agent_root: Path) -> str | None:
    """Move ``data/memory`` → ``memory``. Returns action label or None."""
    legacy = (agent_root / _LEGACY_REL).resolve()
    dest = (agent_root / _CANONICAL_REL).resolve()
    if not legacy.is_dir():
        return None
    if legacy == dest:
        return None

    lock = _acquire_migrate_lock(agent_root)
    if lock is None:
        logger.info(
            "Skipping memory dir move for %s: migrate lock held by another process",
            agent_root.name,
        )
        return "skipped_locked"

    try:
        # Re-check under lock (TOCTOU).
        if not legacy.is_dir():
            return None
        if dest.exists():
            if _is_scaffold_only(dest):
                logger.info(
                    "Merging legacy %s into scaffold-only %s for agent %s",
                    legacy,
                    dest,
                    agent_root.name,
                )
                try:
                    _merge_legacy_into_dest(legacy, dest)
                except OSError as exc:
                    logger.warning(
                        "Failed to merge %s → %s for agent %s: %r",
                        legacy,
                        dest,
                        agent_root.name,
                        exc,
                    )
                    return "move_failed"
                logger.info(
                    "Migrated memory layout for %s: merged %s → %s",
                    agent_root.name,
                    legacy,
                    dest,
                )
                return "moved"
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
    finally:
        _release_migrate_lock(lock)


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
    *, include: Path | None = None, migrate_all: bool | None = None
) -> list[dict[str, Any]]:
    """Migrate agents.

    When ``migrate_all`` is false (default unless ``MONKEYBOT_MIGRATE_ALL_AGENTS=1``),
    only ``include`` is migrated — avoids concurrent Mac/CLI/gateway touching every
    agent under ``~/.monkeybot/agents/``.
    """
    if migrate_all is None:
        migrate_all = os.environ.get("MONKEYBOT_MIGRATE_ALL_AGENTS", "").strip() in (
            "1",
            "true",
            "yes",
        )
    results: list[dict[str, Any]] = []
    roots = (
        discover_local_agent_roots(include=include)
        if migrate_all
        else ([include.expanduser().resolve()] if include is not None else [])
    )
    # When migrate_all is false but include lacks monkeybot_config, still try it
    # if it looks like an agent root (has data/memory or memory).
    if not migrate_all and include is not None:
        root = include.expanduser().resolve()
        if root not in roots:
            roots = [root]

    for root in roots:
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


def migrate_host_memory_volume(
    legacy: Path | str, dest: Path | str
) -> dict[str, Any]:
    """Migrate an absolute host memory dir (docker-compose upgrade path).

    When ``legacy`` exists and ``dest`` is missing or empty, move legacy contents
    into ``dest``. Used by the sandbox compose entrypoint so upgrades do not
    orphan ``/tmp/monkeybot-data/memory``.
    """
    legacy_path = Path(legacy).expanduser()
    dest_path = Path(dest).expanduser()
    result: dict[str, Any] = {
        "legacy": str(legacy_path),
        "dest": str(dest_path),
        "action": "noop",
    }
    if not legacy_path.is_dir():
        dest_path.mkdir(parents=True, exist_ok=True)
        return result

    dest_path.mkdir(parents=True, exist_ok=True)
    try:
        dest_empty = not any(dest_path.iterdir())
    except OSError as exc:
        logger.warning("Cannot list dest memory volume %s: %r", dest_path, exc)
        result["action"] = "error"
        result["error"] = repr(exc)
        return result

    if not dest_empty:
        result["action"] = "skipped_dest_not_empty"
        logger.info(
            "Keeping %s; legacy %s still present (dest not empty)",
            dest_path,
            legacy_path,
        )
        return result

    try:
        for item in legacy_path.iterdir():
            target = dest_path / item.name
            shutil.move(str(item), str(target))
        try:
            legacy_path.rmdir()
        except OSError:
            # Non-empty leftovers (e.g. stuck file) — leave for operator.
            pass
    except OSError as exc:
        logger.warning(
            "Failed host memory migrate %s → %s: %r", legacy_path, dest_path, exc
        )
        result["action"] = "move_failed"
        result["error"] = repr(exc)
        return result

    result["action"] = "moved"
    logger.info("Migrated host memory volume %s → %s", legacy_path, dest_path)
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI for docker-compose host volume migration."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host-legacy",
        type=Path,
        help="Legacy absolute memory directory (e.g. /tmp/monkeybot-data/memory)",
    )
    parser.add_argument(
        "--host-dest",
        type=Path,
        help="Canonical absolute memory directory (e.g. /tmp/monkeybot-memory)",
    )
    args = parser.parse_args(argv)
    if args.host_legacy is None or args.host_dest is None:
        parser.error("--host-legacy and --host-dest are required")
    result = migrate_host_memory_volume(args.host_legacy, args.host_dest)
    action = result.get("action")
    if action == "error" or action == "move_failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "discover_local_agent_roots",
    "migrate_agent_memory_layout",
    "migrate_all_local_agent_memory_layouts",
    "migrate_host_memory_volume",
]
