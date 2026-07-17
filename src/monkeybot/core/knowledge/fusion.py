"""Hybrid fusion: FTS keyword + optional ANN + 1-hop graph expansion + note bias."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex, _content_tokens
from monkeybot.core.knowledge.types import RecallHit, SourceType
from monkeybot.core.persistence.vector_backends import VectorStore

logger = logging.getLogger(__name__)

SourceFilter = Literal["any", "note", "workspace_file"]

_DEFAULT_RRF_K = 20
_STEM_BOOST = 1.3
_PATH_SEGMENT_BOOST = 1.15
_SNIPPET_CHARS = 280

# Noise notes that echo the live chat / auto-capture — keep out of recall.
_NOISE_NOTE_PATHS = frozenset({"memory/chat_log.md", "memory/INDEX.md"})
_NOISE_NOTE_PREFIXES = (
    "memory/raw/",
    "memory/episodic/",
    "memory/semantic/",
)

# Soft-demote ranking traps (lockfiles, tests) without dropping them entirely.
_LOCKFILE_NAMES = frozenset(
    {
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
    }
)
_TEST_PATH_RE = re.compile(
    r"(^|/)(__tests?__/|tests?/)|"
    r"\.(test|spec)\.[^.]+$|"
    r"/(test|tests|spec|specs)/",
    re.IGNORECASE,
)


def _is_noise_path(path: str) -> bool:
    if path in _NOISE_NOTE_PATHS:
        return True
    return any(path.startswith(p) for p in _NOISE_NOTE_PREFIXES)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


def _file_stem(name: str) -> str:
    if "." not in name:
        return name
    return name.rsplit(".", 1)[0]


def _score_multiplier(path: str, *, source_type: SourceType) -> float:
    """Down-rank lockfiles / tests; slight note boost for curated knowledge."""
    name = _basename(path)
    if name in _LOCKFILE_NAMES or name.endswith(".lock"):
        return 0.12
    if _TEST_PATH_RE.search(path):
        return 0.55
    if source_type == "note":
        return 1.12
    return 1.0


def _path_term_boost(path: str, query_tokens: list[str]) -> float:
    """Multiply existing score when a query token matches the filename/path."""
    if not query_tokens:
        return 1.0
    tokens = {t.lower() for t in query_tokens}
    name = _basename(path)
    stem = _file_stem(name)
    if stem in tokens:
        return _STEM_BOOST
    segs: set[str] = set()
    for part in path.replace("\\", "/").lower().split("/"):
        if not part:
            continue
        segs.add(part)
        segs.add(_file_stem(part))
    if tokens & segs:
        return _PATH_SEGMENT_BOOST
    return 1.0


def _graph_adjacency_bonus(rrf_k: int) -> float:
    return 0.2 / (rrf_k + 1)


def _collapse_by_path(hits: list[RecallHit]) -> list[RecallHit]:
    """Keep the best-scoring chunk per path (preserves relative order)."""
    seen: set[str] = set()
    out: list[RecallHit] = []
    for hit in hits:
        if hit.path in seen:
            continue
        seen.add(hit.path)
        out.append(hit)
    return out


def _normalize_scores(hits: list[RecallHit]) -> None:
    """Per-query normalize so the top fused hit is 1.0."""
    if not hits:
        return
    max_score = max(h.score for h in hits)
    if max_score <= 0:
        return
    for hit in hits:
        hit.score = hit.score / max_score


async def recall(
    index: KnowledgeIndex,
    query: str,
    *,
    limit: int = 10,
    path_prefix: str | None = None,
    source: SourceFilter = "any",
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    ann_dimensions: int | None = None,
    rrf_k: int | None = None,
) -> list[RecallHit]:
    """Run keyword FTS (+ optional ANN) + graph expansion and return fused ranked hits."""
    q = (query or "").strip()
    if not q:
        return []

    k = _DEFAULT_RRF_K if rrf_k is None else max(1, int(rrf_k))
    adjacency_bonus = _graph_adjacency_bonus(k)
    query_tokens = _content_tokens(q)

    source_type: SourceType | None
    if source == "any":
        source_type = None
    else:
        source_type = source
    fts_limit = max(limit * 4, 32)
    fts_rows = await index.fts_search(
        q,
        limit=fts_limit,
        path_prefix=path_prefix,
        source=source_type,
    )

    candidates: dict[str, dict[str, Any]] = {}

    for i, row in enumerate(fts_rows):
        path = row["path"]
        if _is_noise_path(path):
            continue
        key = _hit_key(path, row.get("span"))
        bm25 = float(row.get("rank", 0.0))
        if key not in candidates:
            candidates[key] = {
                "path": path,
                "source_type": row["source_type"],
                "snippet": _snippet(row["text"]),
                "span": row.get("span"),
                "links": [],
                "via": None,
                "kw_rank": i + 1,
                "bm25": bm25,
            }
        else:
            candidates[key]["kw_rank"] = min(candidates[key].get("kw_rank", 10**9), i + 1)
            # FTS5 bm25: lower (more negative) is better
            prev = candidates[key].get("bm25")
            if prev is None or bm25 < prev:
                candidates[key]["bm25"] = bm25

    # Optional semantic ANN stage
    if embedding_provider is not None and vector_store is not None:
        try:
            qvec = await embedding_provider.embed_query(q)
            # Over-fetch then path-collapse so demoted lockfile chunks don't dominate.
            ann_hits = await vector_store.query(
                qvec,
                limit=max(fts_limit * 2, 64),
                path_prefix=path_prefix,
                dimensions=ann_dimensions,
            )
            seen_paths: set[str] = set()
            ann_rank = 0
            for hit in ann_hits:
                if _is_noise_path(hit.path):
                    continue
                if _basename(hit.path) in _LOCKFILE_NAMES:
                    continue
                if hit.path in seen_paths:
                    continue
                seen_paths.add(hit.path)
                ann_rank += 1
                span = None
                if hit.start_line is not None:
                    span = {
                        "start_line": int(hit.start_line),
                        "end_line": int(hit.end_line or hit.start_line),
                    }
                key = hit.chunk_id if hit.chunk_id else _hit_key(hit.path, span)
                cosine = float(hit.score)
                if key not in candidates:
                    chunk = await index.get_chunk_snippet(
                        hit.path,
                        start_line=hit.start_line,
                        end_line=hit.end_line,
                    )
                    if chunk is None:
                        continue
                    if source_type is not None and chunk["source_type"] != source_type:
                        continue
                    candidates[key] = {
                        "path": chunk["path"],
                        "source_type": chunk["source_type"],
                        "snippet": _snippet(chunk["text"]),
                        "span": {
                            "start_line": chunk["start_line"],
                            "end_line": chunk["end_line"],
                        },
                        "links": [],
                        "via": "ann",
                        "vec_rank": ann_rank,
                        "cosine": cosine,
                    }
                else:
                    candidates[key]["vec_rank"] = min(
                        candidates[key].get("vec_rank", 10**9), ann_rank
                    )
                    prev_cos = candidates[key].get("cosine")
                    if prev_cos is None or cosine > prev_cos:
                        candidates[key]["cosine"] = cosine
                    if not candidates[key].get("via"):
                        candidates[key]["via"] = "ann"
                if ann_rank >= fts_limit:
                    break
        except Exception as exc:
            logger.warning("knowledge ANN stage failed; continuing keyword+graph: %r", exc)

    # Graph expansion: from note hits, pull 1-hop workspace/note links.
    # Graph targets form a third ranked list fused via RRF (not an absolute score).
    note_paths = {
        c["path"]
        for c in candidates.values()
        if c["source_type"] == "note"
    }
    graph_rank = 0
    for note_path in note_paths:
        edges = await index.links_from(note_path)
        link_refs = [e["ref"] for e in edges]
        for key, cand in list(candidates.items()):
            if cand["path"] == note_path:
                cand["links"] = link_refs

        for edge in edges:
            target = edge["target_path"]
            if path_prefix and not target.startswith(path_prefix):
                continue
            if source == "note" and edge["link_type"] != "note":
                continue
            if source == "workspace_file" and edge["link_type"] != "workspace":
                continue

            span = None
            if edge["start_line"] is not None:
                span = {
                    "start_line": int(edge["start_line"]),
                    "end_line": int(edge["end_line"] or edge["start_line"]),
                }
            key = _hit_key(target, span)
            if _is_noise_path(target):
                continue
            if key in candidates:
                if not candidates[key].get("via"):
                    candidates[key]["via"] = f"graph:{note_path}"
                if "graph_rank" not in candidates[key]:
                    graph_rank += 1
                    candidates[key]["graph_rank"] = graph_rank
                continue

            chunk = await index.get_chunk_snippet(
                target,
                start_line=edge["start_line"],
                end_line=edge["end_line"],
            )
            if chunk is None:
                # Unresolvable target (missing from index) — don't emit null snippets
                continue

            graph_rank += 1
            candidates[key] = {
                "path": chunk["path"],
                "source_type": chunk["source_type"],
                "snippet": _snippet(chunk["text"]),
                "span": {
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                }
                if span is None
                else span,
                "links": [],
                "via": f"graph:{note_path}",
                "graph_rank": graph_rank,
            }

    # Score: RRF across keyword + vector + graph ranks; path-term + demotion multipliers
    scored: list[RecallHit] = []
    for key, cand in candidates.items():
        if _is_noise_path(cand["path"]):
            continue
        score = 0.0
        signals: list[str] = []
        if "kw_rank" in cand:
            score += 1.0 / (k + int(cand["kw_rank"]))
            signals.append("fts")
        if "vec_rank" in cand:
            score += 1.0 / (k + int(cand["vec_rank"]))
            signals.append("ann")
        if "graph_rank" in cand:
            score += 1.0 / (k + int(cand["graph_rank"]))
            signals.append("graph")
            # Small adjacency bonus only when graph reinforces an organic hit
            if "kw_rank" in cand or "vec_rank" in cand:
                score += adjacency_bonus
        if score <= 0:
            continue
        score *= _score_multiplier(cand["path"], source_type=cand["source_type"])
        score *= _path_term_boost(cand["path"], query_tokens)
        scored.append(
            RecallHit(
                source_type=cand["source_type"],
                path=cand["path"],
                score=score,
                snippet=cand.get("snippet"),
                span=cand.get("span"),
                links=list(cand.get("links") or []),
                via=cand.get("via"),
                cosine=cand.get("cosine"),
                bm25=cand.get("bm25"),
                signals=signals,
            )
        )

    scored.sort(
        key=lambda h: (
            -h.score,
            0 if h.source_type == "note" else 1,
            h.path,
        )
    )
    collapsed = _collapse_by_path(scored)[: max(1, limit)]
    _normalize_scores(collapsed)
    return collapsed


def _hit_key(path: str, span: dict[str, int] | None) -> str:
    if span and "start_line" in span:
        end = span.get("end_line", span["start_line"])
        return f"{path}#L{span['start_line']}-{end}"
    return path


def _snippet(text: str, limit: int = _SNIPPET_CHARS) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


__all__ = ["recall"]
