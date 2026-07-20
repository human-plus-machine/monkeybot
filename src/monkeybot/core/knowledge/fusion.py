"""Hybrid fusion: FTS keyword + optional ANN + 1-hop graph expansion + note bias."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from monkeybot.core.lockfile_names import LOCKFILE_NAMES
from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex, _content_tokens
from monkeybot.core.knowledge.types import RecallHit, SourceType
from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

logger = logging.getLogger(__name__)

SourceFilter = Literal["any", "note", "workspace_file"]
Candidate = dict[str, Any]

_DEFAULT_RRF_K = 20
_STEM_BOOST = 1.3
_PATH_SEGMENT_BOOST = 1.15
_SNIPPET_CHARS = 280

_NOISE_NOTE_PREFIXES = (
    "memory/",
)
_TEST_PATH_RE = re.compile(
    r"(^|/)(__tests?__/|tests?/)|"
    r"\.(test|spec)\.[^.]+$|"
    r"/(test|tests|spec|specs)/|"
    r"(^|/)test_[^/]+\.py$|"
    r"(^|/)[^/]+_test\.py$",
    re.IGNORECASE,
)


def _is_noise_path(path: str) -> bool:
    return any(path.startswith(p) for p in _NOISE_NOTE_PREFIXES)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


def _file_stem(name: str) -> str:
    if "." not in name:
        return name
    return name.rsplit(".", 1)[0]


def _score_multiplier(path: str, *, source_type: SourceType) -> float:
    name = _basename(path)
    if name in LOCKFILE_NAMES or name.endswith(".lock"):
        return 0.12
    if _TEST_PATH_RE.search(path):
        return 0.55
    if source_type == "note":
        return 1.12
    return 1.0


def _path_term_boost(path: str, query_tokens: list[str]) -> float:
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
    seen: set[str] = set()
    out: list[RecallHit] = []
    for hit in hits:
        if hit.path in seen:
            continue
        seen.add(hit.path)
        out.append(hit)
    return out


def _normalize_scores(hits: list[RecallHit]) -> None:
    if not hits:
        return
    max_score = max(h.score for h in hits)
    if max_score <= 0:
        return
    for hit in hits:
        hit.score = hit.score / max_score


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


def _ingest_fts(
    candidates: dict[str, Candidate],
    fts_rows: list[dict[str, Any]],
) -> None:
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
            continue
        candidates[key]["kw_rank"] = min(candidates[key].get("kw_rank", 10**9), i + 1)
        prev = candidates[key].get("bm25")
        if prev is None or bm25 < prev:
            candidates[key]["bm25"] = bm25


async def _ingest_ann(
    index: KnowledgeIndex,
    candidates: dict[str, Candidate],
    *,
    query: str,
    fts_limit: int,
    path_prefix: str | None,
    source_type: SourceType | None,
    embedding_provider: NvidiaEmbeddingProvider,
    vector_store: SQLiteVectorStore,
    ann_dimensions: int | None,
) -> None:
    qvec = await embedding_provider.embed_query(query)
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
        if _basename(hit.path) in LOCKFILE_NAMES:
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


async def _expand_graph(
    index: KnowledgeIndex,
    candidates: dict[str, Candidate],
    *,
    path_prefix: str | None,
    source: SourceFilter,
) -> None:
    note_paths = {
        c["path"] for c in candidates.values() if c["source_type"] == "note"
    }
    graph_rank = 0
    # Sorted for stable graph_rank / RRF across process hash seeds.
    for note_path in sorted(note_paths):
        edges = await index.links_from(note_path)
        link_refs = [e["ref"] for e in edges]
        for cand in candidates.values():
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
            if _is_noise_path(target):
                continue

            span = None
            if edge["start_line"] is not None:
                span = {
                    "start_line": int(edge["start_line"]),
                    "end_line": int(edge["end_line"] or edge["start_line"]),
                }
            key = _hit_key(target, span)
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


def _score_candidates(
    candidates: dict[str, Candidate],
    *,
    k: int,
    adjacency_bonus: float,
    query_tokens: list[str],
    limit: int,
) -> list[RecallHit]:
    scored: list[RecallHit] = []
    for cand in candidates.values():
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


async def search(
    index: KnowledgeIndex,
    query: str,
    *,
    limit: int = 10,
    path_prefix: str | None = None,
    source: SourceFilter = "any",
    embedding_provider: NvidiaEmbeddingProvider | None = None,
    vector_store: SQLiteVectorStore | None = None,
    ann_dimensions: int | None = None,
    rrf_k: int | None = None,
) -> list[RecallHit]:
    """Run keyword FTS (+ optional ANN) + graph expansion; return fused ranked hits."""
    q = (query or "").strip()
    if not q:
        return []

    started = time.perf_counter()
    k = _DEFAULT_RRF_K if rrf_k is None else max(1, int(rrf_k))
    adjacency_bonus = _graph_adjacency_bonus(k)
    query_tokens = _content_tokens(q)
    source_type: SourceType | None = None if source == "any" else source
    fts_limit = max(limit * 4, 32)

    fts_rows = await index.fts_search(
        q,
        limit=fts_limit,
        path_prefix=path_prefix,
        source=source_type,
    )
    candidates: dict[str, Candidate] = {}
    _ingest_fts(candidates, fts_rows)

    if embedding_provider is not None and vector_store is not None:
        try:
            await _ingest_ann(
                index,
                candidates,
                query=q,
                fts_limit=fts_limit,
                path_prefix=path_prefix,
                source_type=source_type,
                embedding_provider=embedding_provider,
                vector_store=vector_store,
                ann_dimensions=ann_dimensions,
            )
        except Exception as exc:
            logger.warning("knowledge ANN stage failed; continuing keyword+graph: %r", exc)

    await _expand_graph(
        index, candidates, path_prefix=path_prefix, source=source
    )
    hits = _score_candidates(
        candidates,
        k=k,
        adjacency_bonus=adjacency_bonus,
        query_tokens=query_tokens,
        limit=limit,
    )
    logger.debug(
        "knowledge fusion query_len=%d hits=%d elapsed_ms=%.1f",
        len(q),
        len(hits),
        (time.perf_counter() - started) * 1000,
    )
    return hits


__all__ = ["search"]
