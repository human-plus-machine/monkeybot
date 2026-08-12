"""MemPalace adapter: in-process wake-up / recall / drawer upsert behind a port."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from monkeybot.core.memory.ids import utc_now_iso
from monkeybot.core.memory.observability import log_event

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "embeddinggemma-300m"
DEFAULT_BACKEND = "chroma"
CONVERSATION_ROOM = "conversation"
EMBEDDER_IDENTITY_FILE = ".embedder_identity"
L2_MAX_CHARS = 2000
L2_MAX_DRAWERS = 12


@dataclass(frozen=True)
class DrawerRecord:
    drawer_id: str
    content: str
    wing: str
    room: str
    filed_at: str
    metadata: dict[str, str] = field(default_factory=dict)


class PalacePort(Protocol):
    palace_path: Path
    backend: str
    embedding_model: str

    def ensure_ready(self) -> None: ...

    def upsert_drawer(
        self,
        drawer_id: str,
        content: str,
        metadata: dict[str, str],
    ) -> None: ...

    def get_drawer(self, drawer_id: str) -> DrawerRecord | None: ...

    def wake_up(self, wing: str | None = None, thread_id: str | None = None) -> str: ...

    def recall(
        self,
        *,
        wing: str,
        room: str,
        n_results: int = 10,
        thread_id: str | None = None,
    ) -> list[DrawerRecord]: ...

    def list_drawers(self, *, limit: int = 2000) -> list[DrawerRecord]: ...

    def status(self) -> dict[str, Any]: ...

    def acquire_write_lock(self) -> AbstractContextManager[None]: ...


_UNSUPPORTED_MEMORY_SCHEMES = ("gcs://", "s3://", "gs://")


class UnsupportedMemoryURI(ValueError):
    """Raised when memory_storage_uri uses a scheme MemPalace cannot persist."""


def palace_path_from_uri(memory_uri: str) -> Path:
    raw = memory_uri.strip()
    lowered = raw.lower()
    for scheme in _UNSUPPORTED_MEMORY_SCHEMES:
        if lowered.startswith(scheme):
            raise UnsupportedMemoryURI(
                f"MemPalace does not support {scheme} URIs ({raw!r}); "
                "use a local:// path such as local://./memory/mempalace"
            )
    if "://" in raw and not raw.startswith("local://") and not Path(raw).exists():
        scheme = raw.split("://", 1)[0]
        if scheme not in ("", "file", "local"):
            raise UnsupportedMemoryURI(
                f"MemPalace does not support {scheme}:// URIs ({raw!r}); "
                "use a local:// path such as local://./memory/mempalace"
            )
    if raw.startswith("local://"):
        raw = raw[len("local://") :]
    return Path(raw).expanduser().resolve()


def default_identity_text(agent_name: str) -> str:
    name = agent_name.strip() or "MonkeyBot"
    return (
        f"## L0 — IDENTITY\n"
        f"I am {name}, a MonkeyBot agent.\n"
        f"I remember verbatim conversation turns in this palace.\n"
    )


class InMemoryPalace:
    """Process-local palace used by tests (no embedder, no Chroma)."""

    def __init__(
        self,
        palace_path: Path,
        *,
        agent_name: str = "test-agent",
        backend: str = "memory",
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.palace_path = Path(palace_path)
        self.backend = backend
        self.embedding_model = embedding_model
        self._agent_name = agent_name
        self._lock = threading.Lock()
        self._drawers: dict[str, DrawerRecord] = {}
        self.palace_path.mkdir(parents=True, exist_ok=True)
        identity = self.palace_path / "identity.txt"
        if not identity.is_file():
            identity.write_text(default_identity_text(agent_name), encoding="utf-8")
        (self.palace_path / EMBEDDER_IDENTITY_FILE).write_text(
            embedding_model, encoding="utf-8"
        )

    def ensure_ready(self) -> None:
        return

    @contextmanager
    def acquire_write_lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def upsert_drawer(self, drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        filed_at = metadata.get("filed_at") or metadata.get("source_timestamp") or utc_now_iso()
        record = DrawerRecord(
            drawer_id=drawer_id,
            content=content,
            wing=metadata.get("wing") or "main",
            room=metadata.get("room") or CONVERSATION_ROOM,
            filed_at=filed_at,
            metadata=dict(metadata),
        )
        self._drawers[drawer_id] = record

    def get_drawer(self, drawer_id: str) -> DrawerRecord | None:
        return self._drawers.get(drawer_id)

    def wake_up(self, wing: str | None = None, thread_id: str | None = None) -> str:
        identity = (self.palace_path / "identity.txt").read_text(encoding="utf-8").strip()
        drawers = list(self._drawers.values())
        if wing:
            drawers = [d for d in drawers if d.wing == wing]
        if thread_id:
            drawers = [d for d in drawers if d.metadata.get("thread_id") == thread_id]
        drawers.sort(key=lambda d: d.filed_at, reverse=True)
        lines = [identity, "", "## L1 — ESSENTIAL STORY"]
        if not drawers:
            lines.append("No memories yet.")
            return "\n".join(lines)
        for drawer in drawers[:15]:
            snippet = " ".join(drawer.content.split())
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            lines.append(f"- [{drawer.room}] {snippet}")
        return "\n".join(lines)

    def recall(
        self,
        *,
        wing: str,
        room: str,
        n_results: int = 10,
        thread_id: str | None = None,
    ) -> list[DrawerRecord]:
        matched = [
            d for d in self._drawers.values() if d.wing == wing and d.room == room
        ]
        if thread_id:
            matched = [d for d in matched if d.metadata.get("thread_id") == thread_id]
        matched.sort(key=lambda d: d.filed_at, reverse=True)
        return matched[: max(0, n_results)]

    def list_drawers(self, *, limit: int = 2000) -> list[DrawerRecord]:
        drawers = list(self._drawers.values())
        drawers.sort(key=lambda d: d.filed_at, reverse=True)
        return drawers[: max(0, limit)]

    def status(self) -> dict[str, Any]:
        return {
            "palace_path": str(self.palace_path),
            "total_drawers": len(self._drawers),
            "backend": self.backend,
        }


def _stringify_meta(meta: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (meta or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[str(key)] = str(value)
    return out


class MemPalaceAdapter:
    """Production adapter over MemPalace's Python API and per-palace writer lock."""

    def __init__(
        self,
        palace_path: Path,
        *,
        agent_name: str,
        backend: str = DEFAULT_BACKEND,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.palace_path = Path(palace_path)
        self.backend = backend or DEFAULT_BACKEND
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self._agent_name = agent_name
        self.palace_path.mkdir(parents=True, exist_ok=True)
        identity = self.palace_path / "identity.txt"
        if not identity.is_file():
            identity.write_text(default_identity_text(agent_name), encoding="utf-8")
        self._check_embedder_identity()

    def _check_embedder_identity(self) -> None:
        path = self.palace_path / EMBEDDER_IDENTITY_FILE
        if path.is_file():
            got = path.read_text(encoding="utf-8").strip()
            if got and got != self.embedding_model:
                log_event(
                    "embedder_identity_mismatch",
                    memory_status="error",
                    memory_embedding_model=self.embedding_model,
                    memory_error_class="embedder_mismatch",
                )
                raise RuntimeError(
                    "MemPalace embedder identity mismatch; run `mempalace repair rebuild-index`"
                )
        else:
            path.write_text(self.embedding_model, encoding="utf-8")

    def ensure_ready(self) -> None:
        try:
            from mempalace.layers import MemoryStack

            stack = MemoryStack(
                palace_path=str(self.palace_path),
                identity_path=str(self.palace_path / "identity.txt"),
            )
            stack.wake_up()
            log_event("embedder_warm", memory_status="ok", memory_embedding_model=self.embedding_model)
        except Exception as exc:
            log_event(
                "embedder_warm",
                memory_status="error",
                memory_error_class=type(exc).__name__,
            )
            logger.warning("mempalace embedder warm failed: %r", exc)

    @contextmanager
    def acquire_write_lock(self) -> Iterator[None]:
        from mempalace.palace import mine_palace_lock

        with mine_palace_lock(str(self.palace_path)):
            yield

    def _collection(self, *, create: bool = True) -> Any:
        from mempalace.palace import get_collection

        return get_collection(str(self.palace_path), create=create)

    def upsert_drawer(self, drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        col = self._collection(create=True)
        meta = dict(metadata)
        meta.setdefault("filed_at", utc_now_iso())
        meta.setdefault("added_by", "monkeybot")
        meta.setdefault("chunk_index", "0")
        meta.setdefault("source_file", "")
        col.upsert(ids=[drawer_id], documents=[content], metadatas=[meta])

    def get_drawer(self, drawer_id: str) -> DrawerRecord | None:
        try:
            col = self._collection(create=False)
        except Exception as exc:
            logger.warning("mempalace get_drawer collection failed: %r", exc)
            return None
        try:
            result = col.get(ids=[drawer_id], include=["documents", "metadatas"])
        except Exception as exc:
            logger.warning("mempalace get_drawer failed: %r", exc)
            return None
        ids = result.get("ids") or []
        if not ids:
            return None
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        doc = docs[0] if docs else ""
        meta = _stringify_meta(metas[0] if metas else {})
        return DrawerRecord(
            drawer_id=str(ids[0]),
            content=doc or "",
            wing=meta.get("wing") or "main",
            room=meta.get("room") or CONVERSATION_ROOM,
            filed_at=meta.get("filed_at") or meta.get("source_timestamp") or "",
            metadata=meta,
        )

    def wake_up(self, wing: str | None = None, thread_id: str | None = None) -> str:
        if thread_id:
            identity = (self.palace_path / "identity.txt").read_text(encoding="utf-8").strip()
            drawers = self.recall(
                wing=wing or "main",
                room=CONVERSATION_ROOM,
                n_results=15,
                thread_id=thread_id,
            )
            lines = [identity, "", "## L1 — ESSENTIAL STORY"]
            if not drawers:
                lines.append("No memories yet.")
                return "\n".join(lines)
            for drawer in drawers:
                snippet = " ".join(drawer.content.split())
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."
                lines.append(f"- [{drawer.room}] {snippet}")
            return "\n".join(lines)
        from mempalace.layers import MemoryStack

        stack = MemoryStack(
            palace_path=str(self.palace_path),
            identity_path=str(self.palace_path / "identity.txt"),
        )
        if wing:
            return stack.wake_up(wing=wing)
        return stack.wake_up()

    def recall(
        self,
        *,
        wing: str,
        room: str,
        n_results: int = 10,
        thread_id: str | None = None,
    ) -> list[DrawerRecord]:
        try:
            col = self._collection(create=False)
        except Exception as exc:
            logger.warning("mempalace recall collection failed: %r", exc)
            return []
        clauses: list[dict[str, Any]] = [{"wing": wing}, {"room": room}]
        if thread_id:
            clauses.append({"thread_id": thread_id})
        where: dict[str, Any] = {"$and": clauses}
        try:
            # Fetch metadata only, sort by durable recency, then load the newest N.
            meta_result = col.get(where=where, include=["metadatas"])
        except Exception as exc:
            logger.warning("mempalace recall failed: %r", exc)
            return []
        ids = meta_result.get("ids") or []
        metas = meta_result.get("metadatas") or []
        ranked: list[tuple[str, str, dict[str, str]]] = []
        for drawer_id, meta in zip(ids, metas, strict=False):
            parsed = _stringify_meta(meta)
            filed = parsed.get("source_timestamp") or parsed.get("filed_at") or ""
            ranked.append((str(drawer_id), filed, parsed))
        ranked.sort(key=lambda item: item[1], reverse=True)
        top = ranked[: max(0, n_results)]
        if not top:
            return []
        top_ids = [item[0] for item in top]
        try:
            docs_result = col.get(ids=top_ids, include=["documents", "metadatas"])
        except Exception as exc:
            logger.warning("mempalace recall documents failed: %r", exc)
            return []
        doc_ids = docs_result.get("ids") or []
        docs = docs_result.get("documents") or []
        doc_metas = docs_result.get("metadatas") or []
        by_id: dict[str, tuple[str, dict[str, str]]] = {}
        for drawer_id, doc, meta in zip(doc_ids, docs, doc_metas, strict=False):
            by_id[str(drawer_id)] = (doc or "", _stringify_meta(meta))
        records: list[DrawerRecord] = []
        for drawer_id, filed, parsed in top:
            doc, meta = by_id.get(drawer_id, ("", parsed))
            records.append(
                DrawerRecord(
                    drawer_id=drawer_id,
                    content=doc,
                    wing=meta.get("wing") or wing,
                    room=meta.get("room") or room,
                    filed_at=meta.get("source_timestamp") or meta.get("filed_at") or filed,
                    metadata=meta or parsed,
                )
            )
        return records

    def list_drawers(self, *, limit: int = 2000) -> list[DrawerRecord]:
        try:
            col = self._collection(create=False)
        except Exception as exc:
            logger.warning("mempalace list_drawers collection failed: %r", exc)
            return []
        try:
            result = col.get(include=["documents", "metadatas"])
        except Exception as exc:
            logger.warning("mempalace list_drawers failed: %r", exc)
            return []
        ids = result.get("ids") or []
        docs = result.get("documents") or []
        metas = result.get("metadatas") or []
        records: list[DrawerRecord] = []
        for drawer_id, doc, meta in zip(ids, docs, metas, strict=False):
            parsed = _stringify_meta(meta)
            records.append(
                DrawerRecord(
                    drawer_id=str(drawer_id),
                    content=doc or "",
                    wing=parsed.get("wing") or "main",
                    room=parsed.get("room") or CONVERSATION_ROOM,
                    filed_at=parsed.get("source_timestamp") or parsed.get("filed_at") or "",
                    metadata=parsed,
                )
            )
        records.sort(key=lambda d: d.filed_at, reverse=True)
        return records[: max(0, limit)]

    def status(self) -> dict[str, Any]:
        from mempalace.layers import MemoryStack

        stack = MemoryStack(
            palace_path=str(self.palace_path),
            identity_path=str(self.palace_path / "identity.txt"),
        )
        payload = stack.status()
        payload["backend"] = self.backend
        payload["embedding_model"] = self.embedding_model
        return payload


def format_recall_lines(drawers: list[DrawerRecord], *, max_chars: int = L2_MAX_CHARS) -> list[str]:
    """Bounded L2 lines, newest first. Caller already sorted."""
    lines: list[str] = []
    used = 0
    for drawer in drawers[:L2_MAX_DRAWERS]:
        snippet = " ".join(drawer.content.split())
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        ts = drawer.filed_at[:19] if drawer.filed_at else ""
        role = drawer.metadata.get("role") or ""
        prefix = " ".join(p for p in (ts, role) if p)
        line = f"- [{prefix}] {snippet}" if prefix else f"- {snippet}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return lines


def create_palace(
    memory_uri: str,
    *,
    agent_name: str,
    backend: str | None = None,
    embedding_model: str | None = None,
    in_memory: bool = False,
) -> PalacePort:
    path = palace_path_from_uri(memory_uri)
    be = (backend or os.environ.get("MEMPALACE_BACKEND") or DEFAULT_BACKEND).strip()
    model = (
        embedding_model
        or os.environ.get("MEMPALACE_EMBEDDING_MODEL")
        or DEFAULT_EMBEDDING_MODEL
    ).strip()
    if in_memory:
        return InMemoryPalace(path, agent_name=agent_name, backend="memory", embedding_model=model)
    return MemPalaceAdapter(path, agent_name=agent_name, backend=be, embedding_model=model)
