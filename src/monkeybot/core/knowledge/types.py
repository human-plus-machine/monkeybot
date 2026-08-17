"""Shared types for the unified knowledge layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SourceType = Literal["note", "workspace_file"]
LinkType = Literal["workspace", "note"]


@dataclass(frozen=True)
class TextChunk:
    """One overlapping text span ready for FTS upsert."""

    path: str
    source_type: SourceType
    start_line: int
    end_line: int
    text: str

    @property
    def chunk_id(self) -> str:
        return f"{self.path}#L{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class ParsedLink:
    """One ``[[…]]`` edge extracted from a note body."""

    target_path: str
    link_type: LinkType
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class RecallHit:
    """One fused retrieval hit returned by ``search``."""

    source_type: SourceType
    path: str
    score: float
    snippet: str | None = None
    span: dict[str, int] | None = None
    links: list[str] = field(default_factory=list)
    via: str | None = None
    cosine: float | None = None
    bm25: float | None = None
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source_type": self.source_type,
            "path": self.path,
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }
        if self.span is not None:
            out["span"] = self.span
        if self.links:
            out["links"] = list(self.links)
        if self.via:
            out["via"] = self.via
        if self.cosine is not None:
            out["cosine"] = round(self.cosine, 4)
        if self.bm25 is not None:
            out["bm25"] = round(self.bm25, 4)
        if self.signals:
            out["signals"] = list(self.signals)
        return out


@dataclass(frozen=True)
class EmbeddingSettings:
    """Optional semantic / ANN layer — default off."""

    enabled: bool = False
    provider: str = "nvidia"
    model: str = "nvidia/nemotron-3-embed-1b"
    dimensions: int = 1024
    base_url: str = "https://integrate.api.nvidia.com/v1"
    batch_size: int = 32


@dataclass(frozen=True)
class VectorStoreSettings:
    """Pluggable similarity store (sqlite default for Phase 2)."""

    type: str = "sqlite"
    path: str = ".monkeybot/knowledge/vectors.sqlite"


CaptionMode = Literal["off", "path", "llm"]


@dataclass(frozen=True)
class KnowledgeSettings:
    """Resolved knowledge-layer settings for one agent process."""

    enabled: bool = True
    knowledge_root: str = ".monkeybot/knowledge"
    index_path: str = ".monkeybot/knowledge/index.sqlite"
    default_limit: int = 10
    debounce_ms: int = 300
    startup_scan: bool = True
    max_file_bytes: int = 5_000_000
    chunk_tokens: int = 700
    chunk_overlap_ratio: float = 0.12
    rrf_k: int = 20
    captions: CaptionMode = "path"
    caption_model: str | None = None
    embeddings: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    store: VectorStoreSettings = field(default_factory=VectorStoreSettings)


__all__ = [
    "CaptionMode",
    "EmbeddingSettings",
    "KnowledgeSettings",
    "LinkType",
    "ParsedLink",
    "RecallHit",
    "SourceType",
    "TextChunk",
    "VectorStoreSettings",
]
