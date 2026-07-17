"""Config helpers for the unified knowledge layer."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict
from monkeybot.core.knowledge.types import (
    EmbeddingSettings,
    KnowledgeSettings,
    VectorStoreSettings,
)
from monkeybot.core.layout import resolve_agent_path, resolve_agent_root, resolve_config_path

logger = logging.getLogger(__name__)


def knowledge_enabled_from_env() -> bool:
    """``KNOWLEDGE_ENABLED`` — default true when unset."""
    raw = os.environ.get("KNOWLEDGE_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def knowledge_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether the unified knowledge layer is enabled (YAML, default true)."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    section = doc.get("knowledge")
    if not isinstance(section, dict):
        return knowledge_enabled_from_env()
    raw = section.get("enabled")
    if raw is None:
        return knowledge_enabled_from_env()
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"knowledge.enabled must be true or false, got {raw!r}")


def resolve_knowledge_settings(
    *,
    agent_root: Path | None = None,
    config_path: Path | None = None,
    workspace_root: Path | None = None,
) -> KnowledgeSettings:
    """Resolve knowledge settings from YAML + env.

    Relative paths are anchored at the workspace root when known (prefer
    ``workspace/.monkeybot/knowledge``), otherwise at the agent root.
    Absolute ``KNOWLEDGE_LOCAL_INDEX_PATH`` still overrides.
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

    # Prefer workspace/.monkeybot/knowledge when workspace_root is known
    anchor = Path(workspace_root).resolve() if workspace_root is not None else root
    knowledge_root = resolve_agent_path(knowledge_root_raw, anchor)

    local_index_raw = section.get("local_index")
    local_index: dict[str, Any] = local_index_raw if isinstance(local_index_raw, dict) else {}
    index_raw = os.environ.get("KNOWLEDGE_LOCAL_INDEX_PATH", "").strip()
    if not index_raw:
        index_raw = (
            str(local_index.get("path")).strip()
            if isinstance(local_index.get("path"), str)
            else ""
        )
    if not index_raw:
        index_raw = str(knowledge_root / "index.sqlite")
    index_path = resolve_agent_path(index_raw, anchor)

    _maybe_migrate_legacy_index(agent_root=root, index_path=index_path)

    search_raw = section.get("search")
    if not isinstance(search_raw, dict):
        search_raw = section.get("recall")  # legacy key
    search: dict[str, Any] = search_raw if isinstance(search_raw, dict) else {}
    default_limit = _int(
        os.environ.get("KNOWLEDGE_SEARCH_DEFAULT_LIMIT")
        or os.environ.get("KNOWLEDGE_RECALL_DEFAULT_LIMIT"),
        search.get("default_limit"),
        10,
    )

    indexer_raw = section.get("indexer")
    indexer: dict[str, Any] = indexer_raw if isinstance(indexer_raw, dict) else {}
    debounce_ms = _int(
        os.environ.get("KNOWLEDGE_INDEXER_DEBOUNCE_MS"),
        indexer.get("debounce_ms"),
        300,
    )
    startup_scan = _bool(indexer.get("startup_scan"), True)
    max_file_bytes = _int(
        os.environ.get("KNOWLEDGE_MAX_FILE_BYTES"),
        indexer.get("max_file_bytes"),
        5_000_000,
    )
    chunk_tokens = _int(None, indexer.get("chunk_tokens"), 700)
    overlap = _float(indexer.get("chunk_overlap_ratio"), 0.12)
    rrf_k = _int(
        os.environ.get("KNOWLEDGE_RRF_K"),
        search.get("rrf_k"),
        20,
    )

    embeddings = _resolve_embeddings(section.get("embeddings"))
    store = _resolve_store(section.get("store"), knowledge_root=knowledge_root, anchor=anchor)

    return KnowledgeSettings(
        enabled=enabled,
        knowledge_root=str(knowledge_root),
        index_path=str(index_path),
        default_limit=default_limit,
        debounce_ms=debounce_ms,
        startup_scan=startup_scan,
        max_file_bytes=max_file_bytes,
        chunk_tokens=chunk_tokens,
        chunk_overlap_ratio=overlap,
        rrf_k=rrf_k,
        embeddings=embeddings,
        store=store,
    )


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


def _resolve_embeddings(raw: Any) -> EmbeddingSettings:
    section: dict[str, Any] = raw if isinstance(raw, dict) else {}
    env_enabled = os.environ.get("KNOWLEDGE_EMBEDDINGS_ENABLED", "").strip().lower()
    if env_enabled in {"1", "true", "yes", "on"}:
        enabled = True
    elif env_enabled in {"0", "false", "no", "off"}:
        enabled = False
    else:
        enabled = _bool(section.get("enabled"), False)

    provider = "nvidia"
    if isinstance(section.get("provider"), str) and section["provider"].strip():
        provider = section["provider"].strip()

    model = "nvidia/nemotron-3-embed-1b"
    if isinstance(section.get("model"), str) and section["model"].strip():
        model = section["model"].strip()

    dimensions = _int(None, section.get("dimensions"), 1024)
    base_url = "https://integrate.api.nvidia.com/v1"
    if isinstance(section.get("base_url"), str) and section["base_url"].strip():
        base_url = section["base_url"].strip().rstrip("/")
    batch_size = _int(None, section.get("batch_size"), 32)

    return EmbeddingSettings(
        enabled=enabled,
        provider=provider,
        model=model,
        dimensions=dimensions,
        base_url=base_url,
        batch_size=batch_size,
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


def _int(env_raw: str | None, yaml_raw: Any, default: int) -> int:
    if env_raw is not None and str(env_raw).strip():
        try:
            return int(str(env_raw).strip())
        except ValueError:
            pass
    if isinstance(yaml_raw, int):
        return yaml_raw
    if isinstance(yaml_raw, str) and yaml_raw.strip():
        try:
            return int(yaml_raw.strip())
        except ValueError:
            pass
    return default


def _float(yaml_raw: Any, default: float) -> float:
    if isinstance(yaml_raw, (int, float)):
        return float(yaml_raw)
    if isinstance(yaml_raw, str) and yaml_raw.strip():
        try:
            return float(yaml_raw.strip())
        except ValueError:
            pass
    return default


def _bool(yaml_raw: Any, default: bool) -> bool:
    if isinstance(yaml_raw, bool):
        return yaml_raw
    return default


__all__ = [
    "knowledge_enabled_from_config",
    "knowledge_enabled_from_env",
    "resolve_knowledge_settings",
]
