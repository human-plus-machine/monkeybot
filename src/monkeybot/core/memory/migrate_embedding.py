"""Point palaces configured as ``embeddinggemma-300m`` at MiniLM.

That string is not a MemPalace model name. Older builds accepted it and filed
the palace with MiniLM, which is MemPalace's default. MemPalace 3.11 refuses
the name. Startup rewrites it to ``minilm`` and records the same name in the
palace identity file. The vectors already match, so the index is not rebuilt.

The migration walks every agent under ``MONKEYBOT_HOME`` (default
``~/.monkeybot``) plus the agent that is starting. A config that names
``embeddinggemma``, ``embeddinggemma2``, or ``openai-compat`` is left alone.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from monkeybot.core.layout import resolve_memory_storage_uri
from monkeybot.core.memory.migrate_layout import discover_local_agent_roots
from monkeybot.core.memory.palace import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDER_IDENTITY_FILE,
    palace_path_from_uri,
)

logger = logging.getLogger(__name__)

TARGET_EMBEDDING_MODEL = DEFAULT_EMBEDDING_MODEL
LEGACY_EMBEDDING_MODELS = frozenset({"embeddinggemma-300m", "embeddinggemma_300m"})
# A real MemPalace model. Switching one of these to MiniLM would orphan its vectors.
PRESERVE_EMBEDDING_MODELS = frozenset({"embeddinggemma", "embeddinggemma2", "openai-compat"})
_LEGACY_MODEL_LINE = re.compile(
    r"^(\s*embedding_model:\s*)(?:[\"']?)"
    r"(?:embeddinggemma-300m|embeddinggemma_300m)"
    r"(?:[\"']?)\s*$",
    re.IGNORECASE,
)


def rewrite_legacy_embedding_model(text: str, *, model: str = TARGET_EMBEDDING_MODEL) -> str:
    """Replace a legacy ``embedding_model`` value. Other lines stay put."""
    if not text:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    ends_newline = text.endswith("\n")
    changed = False
    out: list[str] = []
    for line in text.splitlines():
        match = _LEGACY_MODEL_LINE.match(line)
        if match is None:
            out.append(line)
            continue
        out.append(f"{match.group(1)}{model}")
        changed = True
    if not changed:
        return text
    rewritten = newline.join(out)
    if ends_newline:
        rewritten += newline
    return rewritten


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Cannot read %s for embedding migration: %r", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _configured_model(doc: dict[str, Any]) -> str:
    memory = doc.get("memory")
    if not isinstance(memory, dict):
        return ""
    raw = memory.get("embedding_model")
    if raw is None:
        return ""
    return str(raw).strip()


def _palace_for(agent_root: Path, doc: dict[str, Any]) -> Path | None:
    paths = doc.get("paths")
    raw = ""
    if isinstance(paths, dict):
        raw = str(paths.get("memory_storage_uri") or "")
    if not raw.strip():
        raw = "local://./memory/mempalace"
    try:
        return palace_path_from_uri(resolve_memory_storage_uri(raw, agent_root))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot resolve palace for %s: %r", agent_root, exc)
        return None


def _read_identity(palace: Path | None) -> str:
    if palace is None:
        return ""
    path = palace / EMBEDDER_IDENTITY_FILE
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Cannot read %s: %r", path, exc)
        return ""


def _needs_migration(*, configured: str, identity: str) -> bool:
    cfg = configured.strip().lower()
    recorded = identity.strip().lower()
    if cfg in PRESERVE_EMBEDDING_MODELS:
        return False
    return cfg in LEGACY_EMBEDDING_MODELS or recorded in LEGACY_EMBEDDING_MODELS


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_identity(palace: Path) -> None:
    _write_text(palace / EMBEDDER_IDENTITY_FILE, TARGET_EMBEDDING_MODEL + "\n")


def _rewrite_config_files(config_dir: Path) -> bool:
    changed = False
    for name in ("monkeybot.yaml", "monkeybot.example.yaml"):
        path = config_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot read %s: %r", path, exc)
            continue
        rewritten = rewrite_legacy_embedding_model(text)
        if rewritten == text:
            continue
        try:
            _write_text(path, rewritten)
        except OSError as exc:
            logger.warning("Cannot write %s: %r", path, exc)
            continue
        changed = True
    return changed


@contextmanager
def _migration_lock(palace: Path) -> Iterator[None]:
    """Serialize two gateways that both start on the update that renames the model."""
    import fcntl

    lock_path = palace / ".embedding-migrate.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _migrate_agent_embedding(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "agent_root": str(root),
        "action": "noop",
        "yaml_updated": False,
        "identity_updated": False,
    }
    config_dir = root / "monkeybot_config"
    doc = _load_yaml(config_dir / "monkeybot.yaml")
    configured = _configured_model(doc)
    palace = _palace_for(root, doc)
    identity = _read_identity(palace)
    if not _needs_migration(configured=configured, identity=identity):
        return result

    if (
        palace is not None
        and palace.is_dir()
        and identity.strip().lower() in LEGACY_EMBEDDING_MODELS
    ):
        try:
            _write_identity(palace)
        except OSError as exc:
            logger.warning("Cannot record embedder for %s: %r", palace, exc)
            result["action"] = "failed"
            return result
        result["identity_updated"] = True

    result["yaml_updated"] = _rewrite_config_files(config_dir)
    result["action"] = "migrated"
    logger.info("Recorded MiniLM as the embedder for %s", root.name)
    return result


def migrate_agent_embedding(agent_root: Path) -> dict[str, Any]:
    """Rewrite one agent's legacy embedder name to ``minilm``."""
    root = agent_root.expanduser().resolve()
    doc = _load_yaml(root / "monkeybot_config" / "monkeybot.yaml")
    palace = _palace_for(root, doc)
    if palace is not None and palace.is_dir():
        with _migration_lock(palace):
            return _migrate_agent_embedding(root)
    return _migrate_agent_embedding(root)


def migrate_all_agent_embeddings(*, include: Path | None = None) -> list[dict[str, Any]]:
    """Migrate every local agent, plus ``include`` when it lives elsewhere."""
    results: list[dict[str, Any]] = []
    for root in discover_local_agent_roots(include=include):
        try:
            results.append(migrate_agent_embedding(root))
        except Exception as exc:  # noqa: BLE001 — one agent must not block the others
            logger.warning("Embedding migration failed for %s: %r", root, exc)
            results.append(
                {
                    "agent_root": str(root),
                    "action": "failed",
                    "yaml_updated": False,
                    "identity_updated": False,
                }
            )
    return results


def main(argv: list[str] | None = None) -> int:
    """Rename legacy embedder configs. Exits 1 when any agent fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        type=Path,
        action="append",
        default=[],
        help="Agent root to migrate. Repeat to name more than one. "
        "With no --agent, every agent under MONKEYBOT_HOME is migrated.",
    )
    args = parser.parse_args(argv)
    if args.agent:
        results = [migrate_agent_embedding(path) for path in args.agent]
    else:
        results = migrate_all_agent_embeddings()
    failed = [item for item in results if item.get("action") == "failed"]
    migrated = [item for item in results if item.get("action") == "migrated"]
    logger.info(
        "Embedding migration finished: %d migrated, %d failed, %d checked",
        len(migrated),
        len(failed),
        len(results),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LEGACY_EMBEDDING_MODELS",
    "TARGET_EMBEDDING_MODEL",
    "migrate_agent_embedding",
    "migrate_all_agent_embeddings",
    "rewrite_legacy_embedding_model",
]
