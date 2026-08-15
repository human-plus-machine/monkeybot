"""MemPalace adapter: in-process wake-up / recall / drawer upsert behind a port."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

from monkeybot.core.memory.ids import utc_now_iso
from monkeybot.core.memory.observability import log_event
from monkeybot.core.memory.uri import object_store_memory_scheme

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "embeddinggemma-300m"
DEFAULT_BACKEND = "chroma"
CONVERSATION_ROOM = "conversation"
EMBEDDER_IDENTITY_FILE = ".embedder_identity"
PALACE_ID_FILE = ".palace_id"
PALACE_WRITE_LOCK_FILE = ".palace_write.lock"
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

    def wake_up(self, wing: str | None = None) -> str: ...

    def recall(
        self,
        *,
        wing: str,
        room: str,
        n_results: int = 10,
        thread_id: str | None = None,
    ) -> list[DrawerRecord]: ...

    def status(self) -> dict[str, Any]: ...

    def acquire_write_lock(self) -> AbstractContextManager[None]: ...


class MemoryDependencyError(RuntimeError):
    """Raised when MemPalace-backed memory is enabled without ``monkeybot[memory]``."""


def mempalace_available() -> bool:
    """Whether the optional MemPalace runtime is installed."""
    try:
        return find_spec("mempalace") is not None
    except (ImportError, ValueError):
        return False


def palace_path_from_uri(memory_uri: str) -> Path:
    raw = memory_uri.strip()
    if not raw:
        raise ValueError("memory URI is empty")
    remote = object_store_memory_scheme(raw)
    if remote:
        raise ValueError(
            f"unsupported memory URI {memory_uri!r}; MemPalace does not support "
            f"{remote} (use local://)"
        )
    scheme, _, rest = raw.partition("://")
    path = rest if rest and scheme.lower() in {"local", "file"} else raw
    return Path(path).expanduser().resolve()


@contextmanager
def palace_volume_lock(palace_path: Path) -> Iterator[None]:
    """Serialize palace mutations using a POSIX flock on the palace volume."""
    import fcntl

    palace_path.mkdir(parents=True, exist_ok=True)
    lock_path = palace_path / PALACE_WRITE_LOCK_FILE
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_palace_id(path: Path, token: str, *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    else:
        flags |= os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())


def palace_instance_id(palace_path: Path) -> str:
    """Stable id for this palace directory (shared volume ⇒ same id).

    Reads ``.palace_id`` without the volume lock. The flock is taken only on the
    create path so constructing a subsystem does not stall behind an in-flight
    embedding upsert.
    """
    path = palace_path / PALACE_ID_FILE
    if path.is_file():
        got = path.read_text(encoding="utf-8").strip()
        if got:
            return got
    path.parent.mkdir(parents=True, exist_ok=True)
    with palace_volume_lock(path.parent):
        if path.is_file():
            got = path.read_text(encoding="utf-8").strip()
            if got:
                return got
        token = uuid.uuid4().hex
        try:
            _write_palace_id(path, token, exclusive=True)
        except FileExistsError:
            got = path.read_text(encoding="utf-8").strip()
            if got:
                return got
            _write_palace_id(path, token, exclusive=False)
        return path.read_text(encoding="utf-8").strip()


def palace_path_is_ephemeral(palace_path: Path) -> bool:
    """True when the palace lives under the process temp directory."""
    resolved = Path(palace_path).expanduser().resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(tmp)
        return True
    except ValueError:
        return False


def default_identity_text(agent_name: str) -> str:
    name = agent_name.strip() or "MonkeyBot"
    return (
        f"## L0 — IDENTITY\n"
        f"I am {name}, a MonkeyBot agent.\n"
        f"I remember verbatim conversation turns in this palace.\n"
    )


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
            log_event(
                "embedder_warm", memory_status="ok", memory_embedding_model=self.embedding_model
            )
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

        with palace_volume_lock(self.palace_path), mine_palace_lock(str(self.palace_path)):
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

    def wake_up(self, wing: str | None = None) -> str:
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
) -> PalacePort:
    path = palace_path_from_uri(memory_uri)
    be = (backend or os.environ.get("MEMPALACE_BACKEND") or DEFAULT_BACKEND).strip()
    model = (
        embedding_model or os.environ.get("MEMPALACE_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    ).strip()
    if not mempalace_available():
        raise MemoryDependencyError(
            "MemPalace memory requires the optional dependency; install `monkeybot[memory]`"
        )
    return MemPalaceAdapter(path, agent_name=agent_name, backend=be, embedding_model=model)
