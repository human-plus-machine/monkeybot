"""Config helpers for the unified knowledge layer (YAML only)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict
from monkeybot.core.knowledge.types import (
    CaptionMode,
    EmbeddingSettings,
    KnowledgeSettings,
    VectorStoreSettings,
)
from monkeybot.core.layout import resolve_agent_path, resolve_agent_root, resolve_config_path

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_MS = 300
_DEFAULT_STARTUP_SCAN = True
_DEFAULT_MAX_FILE_BYTES = 5_000_000
_DEFAULT_INDEX_NAME = "index.sqlite"


def knowledge_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether the unified knowledge layer is enabled (YAML ``knowledge.enabled``, default true)."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    section = doc.get("knowledge")
    if not isinstance(section, dict):
        return True
    raw = section.get("enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"knowledge.enabled must be true or false, got {raw!r}")


def resolve_knowledge_settings(
    *,
    agent_root: Path | None = None,
    config_path: Path | None = None,
    workspace_root: Path | None = None,
) -> KnowledgeSettings:
    """Resolve knowledge settings from YAML only.

    Index path is always ``{knowledge_root}/index.sqlite``. Debounce, startup
    scan, and max file bytes are fixed defaults (not configurable).

    Side effect: may move a legacy agent-root index (and WAL/SHM sidecars) into
    the workspace knowledge root via ``_maybe_migrate_legacy_index``.
    """
    root = (agent_root or resolve_agent_root(config_path=config_path)).resolve()
    cfg = config_path or resolve_config_path(agent_root=root)
    _, doc = load_monkeybot_yaml_dict(str(cfg) if cfg else None)
    section: dict[str, Any] = {}
    raw_section = doc.get("knowledge") if isinstance(doc, dict) else None
    if isinstance(raw_section, dict):
        section = raw_section

    enabled = knowledge_enabled_from_config(str(cfg) if cfg else None)

    knowledge_root_raw = ".monkeybot/knowledge"
    if isinstance(section.get("root"), str) and section["root"].strip():
        knowledge_root_raw = section["root"].strip()

    anchor = Path(workspace_root).resolve() if workspace_root is not None else root
    knowledge_root = resolve_agent_path(knowledge_root_raw, anchor)
    index_path = knowledge_root / _DEFAULT_INDEX_NAME

    _maybe_migrate_legacy_index(agent_root=root, index_path=index_path)

    search_raw = section.get("search")
    if not isinstance(search_raw, dict):
        search_raw = section.get("recall")  # legacy key
    search: dict[str, Any] = search_raw if isinstance(search_raw, dict) else {}
    default_limit = _int(search.get("default_limit"), 10)
    rrf_k = _int(search.get("rrf_k"), 20)

    indexer_raw = section.get("indexer")
    indexer: dict[str, Any] = indexer_raw if isinstance(indexer_raw, dict) else {}
    chunk_tokens = _int(indexer.get("chunk_tokens"), 700)
    overlap = _float(indexer.get("chunk_overlap_ratio"), 0.12)

    embeddings = _resolve_embeddings(section.get("embeddings"))
    store = _resolve_store(section.get("store"), knowledge_root=knowledge_root, anchor=anchor)
    captions, caption_model = _resolve_captions(section)

    return KnowledgeSettings(
        enabled=enabled,
        knowledge_root=str(knowledge_root),
        index_path=str(index_path),
        default_limit=default_limit,
        debounce_ms=_DEFAULT_DEBOUNCE_MS,
        startup_scan=_DEFAULT_STARTUP_SCAN,
        max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
        chunk_tokens=chunk_tokens,
        chunk_overlap_ratio=overlap,
        rrf_k=rrf_k,
        captions=captions,
        caption_model=caption_model,
        embeddings=embeddings,
        store=store,
    )


_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _maybe_migrate_legacy_index(*, agent_root: Path, index_path: Path) -> None:
    """Move agent-root index into the workspace path when the latter is missing."""
    legacy = (agent_root / ".monkeybot" / "knowledge" / "index.sqlite").resolve()
    dest = index_path.resolve()
    if not legacy.is_file() or legacy == dest:
        return
    if not dest.is_file():
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(dest))
            # Move WAL/SHM/journal sidecars too — leaving them behind would
            # drop any uncommitted frames from an unclean prior shutdown and
            # silently reopen the migrated DB missing recent writes.
            for suffix in _SQLITE_SIDECAR_SUFFIXES:
                side = legacy.with_name(legacy.name + suffix)
                if side.is_file():
                    shutil.move(str(side), str(dest.with_name(dest.name + suffix)))
            logger.info("Migrated knowledge index from %s to %s", legacy, dest)
        except OSError as exc:
            logger.warning(
                "Failed to migrate knowledge index from %s to %s: %r",
                legacy,
                dest,
                exc,
            )
        return
    logger.warning(
        "Stale knowledge index at %s (using workspace index at %s). "
        "Delete the agent-root .monkeybot/knowledge copy to avoid split-brain.",
        legacy,
        dest,
    )


def _resolve_captions(section: dict[str, Any]) -> tuple[CaptionMode, str | None]:
    """Resolve ``knowledge.captions`` / legacy ``knowledge.caption`` + caption_model."""
    mode_raw = section.get("captions")
    if mode_raw is None:
        mode_raw = section.get("caption")  # legacy singular key
    mode: CaptionMode = "path"
    if isinstance(mode_raw, str) and mode_raw.strip():
        normalized = mode_raw.strip().lower()
        if normalized in {"off", "path", "llm"}:
            mode = normalized  # type: ignore[assignment]
        else:
            logger.warning("knowledge.captions invalid %r; using default=path", mode_raw)
    caption_model: str | None = None
    if isinstance(section.get("caption_model"), str) and section["caption_model"].strip():
        caption_model = section["caption_model"].strip()
    return mode, caption_model


def _resolve_embeddings(raw: Any) -> EmbeddingSettings:
    from monkeybot.core.knowledge.embeddings.factory import apply_provider_defaults

    section: dict[str, Any] = raw if isinstance(raw, dict) else {}
    enabled = _bool(section.get("enabled"), False)

    provider = "nvidia"
    if isinstance(section.get("provider"), str) and section["provider"].strip():
        provider = section["provider"].strip()

    model: str | None = None
    if isinstance(section.get("model"), str) and section["model"].strip():
        model = section["model"].strip()

    dimensions_raw = section.get("dimensions")
    dimensions: int | None = None
    if dimensions_raw is not None:
        dimensions = _int(dimensions_raw, 0) or None

    base_url: str | None = None
    if isinstance(section.get("base_url"), str) and section["base_url"].strip():
        base_url = section["base_url"].strip().rstrip("/")

    batch_size = _int(section.get("batch_size"), 32)
    filled = apply_provider_defaults(
        provider=provider,
        model=model,
        dimensions=dimensions,
        base_url=base_url,
        batch_size=batch_size,
    )
    return EmbeddingSettings(
        enabled=enabled,
        provider=str(filled["provider"]),
        model=str(filled["model"]),
        dimensions=int(filled["dimensions"]),
        base_url=str(filled["base_url"]),
        batch_size=int(filled["batch_size"]),
    )


def _resolve_store(
    raw: Any,
    *,
    knowledge_root: Path,
    anchor: Path,
) -> VectorStoreSettings:
    section: dict[str, Any] = raw if isinstance(raw, dict) else {}
    store_type = "sqlite"
    if isinstance(section.get("type"), str) and section["type"].strip():
        store_type = section["type"].strip()

    path_raw = ""
    if isinstance(section.get("path"), str) and section["path"].strip():
        path_raw = section["path"].strip()
    if not path_raw:
        path_raw = str(knowledge_root / "vectors.sqlite")
    path = resolve_agent_path(path_raw, anchor)
    return VectorStoreSettings(type=store_type, path=str(path))


def _int(yaml_raw: Any, default: int) -> int:
    if isinstance(yaml_raw, int):
        return yaml_raw
    if isinstance(yaml_raw, str) and yaml_raw.strip():
        try:
            return int(yaml_raw.strip())
        except ValueError:
            logger.warning(
                "knowledge config int yaml invalid %r; using default=%s", yaml_raw, default
            )
    return default


def _float(yaml_raw: Any, default: float) -> float:
    if isinstance(yaml_raw, (int, float)):
        return float(yaml_raw)
    if isinstance(yaml_raw, str) and yaml_raw.strip():
        try:
            return float(yaml_raw.strip())
        except ValueError:
            logger.warning(
                "knowledge config float yaml invalid %r; using default=%s", yaml_raw, default
            )
    return default


def _bool(yaml_raw: Any, default: bool) -> bool:
    if isinstance(yaml_raw, bool):
        return yaml_raw
    return default


__all__ = [
    "knowledge_enabled_from_config",
    "resolve_knowledge_settings",
]
