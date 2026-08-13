"""P1/P2 review coverage for MemPalace isolation, durability, and lifecycle."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.memory.config import memory_enabled_from_config
from monkeybot.core.memory.import_notes import import_legacy_notes, migrate_memory_uri_in_yaml
from monkeybot.core.memory.outbox import (
    STATUS_DEAD,
    STATUS_PENDING,
    ensure_outbox_schema,
    insert_pending,
    is_permanent_error,
    mark_retry,
)
from monkeybot.core.memory.palace import (
    InMemoryPalace,
    MemPalaceAdapter,
    UnsupportedMemoryURI,
    palace_path_from_uri,
)
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend


@pytest.fixture
async def db_url(tmp_path: Path) -> str:
    path = tmp_path / "monkeybot.db"
    url = f"sqlite:///{path}"
    conn = await open_connection(url)
    await apply_schema(conn)
    await ensure_outbox_schema(conn)
    await conn.close()
    return url


def _subsystem(
    tmp_path: Path,
    db_url: str,
    *,
    agent_id: str = "agent-a",
    palace: InMemoryPalace | None = None,
    storage: Any | None = None,
    outbox: Any | None = None,
) -> MemorySubsystem:
    palace = palace or InMemoryPalace(tmp_path / agent_id / "mempalace", agent_name=agent_id)
    return MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=db_url,
        agent_id=agent_id,
        agent_name=agent_id,
        palace=palace,
        storage=storage,
        outbox=outbox,
    )


@pytest.mark.asyncio
async def test_recall_is_thread_scoped(db_url: str, tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t1", turn_id="1", message_id="m1", role="user", content="alpha secret"
    )
    await sub.enqueue(
        thread_id="t2", turn_id="1", message_id="m2", role="user", content="beta secret"
    )
    await sub.drain_writer(timeout_s=2)
    t1 = await sub.recall(wing="main", room="conversation", thread_id="t1")
    t2 = await sub.recall(wing="main", room="conversation", thread_id="t2")
    assert any("alpha secret" in d.content for d in t1)
    assert not any("beta secret" in d.content for d in t1)
    assert any("beta secret" in d.content for d in t2)
    assert not any("alpha secret" in d.content for d in t2)
    wake = "\n".join(await sub.load_index(thread_id="t1"))
    assert "alpha secret" in wake
    assert "beta secret" not in wake
    payload = HookPayload(
        event=HookEvent.PRE_TURN,
        thread_id="t2",
        request_id="r",
        ctx=MagicMock(),
        user_message="hi",
    )
    await sub._hook.on_pre_turn(payload)
    injected = "\n".join(payload.inject_memory_lines)
    assert "beta secret" in injected
    assert "alpha secret" not in injected
    await sub.close()


@pytest.mark.asyncio
async def test_shared_sqlite_outbox_is_agent_scoped(db_url: str, tmp_path: Path) -> None:
    a = _subsystem(tmp_path, db_url, agent_id="agent-a")
    b = _subsystem(tmp_path, db_url, agent_id="agent-b")
    await a.ensure_ready()
    await b.ensure_ready()
    await a.enqueue(thread_id="t", turn_id="1", message_id="m", role="user", content="secret for A")
    await b.enqueue(thread_id="t", turn_id="1", message_id="m", role="user", content="secret for B")
    await asyncio.gather(a.drain_writer(timeout_s=2), b.drain_writer(timeout_s=2))
    a_text = "\n".join(d.content for d in await a.recall(wing="main", room="conversation"))
    b_text = "\n".join(d.content for d in await b.recall(wing="main", room="conversation"))
    assert "secret for A" in a_text
    assert "secret for B" not in a_text
    assert "secret for B" in b_text
    assert "secret for A" not in b_text
    await a.close()
    await b.close()


class _PoisonPalace(InMemoryPalace):
    def upsert_drawer(self, drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        if "POISON" in content:
            raise ValueError("bad drawer")
        super().upsert_drawer(drawer_id, content, metadata)


@pytest.mark.asyncio
async def test_poison_row_does_not_dead_letter_siblings(db_url: str, tmp_path: Path) -> None:
    palace = _PoisonPalace(tmp_path / "poison" / "mempalace", agent_name="agent-a")
    sub = _subsystem(tmp_path, db_url, palace=palace)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t", turn_id="1", message_id="ok", role="user", content="healthy row"
    )
    await sub.enqueue(
        thread_id="t", turn_id="1", message_id="bad", role="user", content="POISON drawer"
    )
    await sub.drain_writer(timeout_s=2)
    drawers = await sub.recall(wing="main", room="conversation")
    assert any("healthy row" in d.content for d in drawers)
    assert not any("POISON" in d.content for d in drawers)
    store = await sub._ensure_outbox()
    assert await store.dead_depth(agent_id="agent-a") == 1
    pending, _ = await store.pending_depth(agent_id="agent-a")
    assert pending == 0
    await sub.close()


class _SlowPalace(InMemoryPalace):
    def __init__(self, palace_path: Path, **kwargs: Any) -> None:
        super().__init__(palace_path, **kwargs)
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock_thread_ident: int | None = None

    def upsert_drawer(self, drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        self.lock_thread_ident = threading.get_ident()
        self.started.set()
        assert self.release.wait(timeout=5)
        super().upsert_drawer(drawer_id, content, metadata)


@pytest.mark.asyncio
async def test_stop_awaits_in_flight_write_on_worker_thread(db_url: str, tmp_path: Path) -> None:
    from monkeybot.core.memory.writer import MemoryWriter

    palace = _SlowPalace(tmp_path / "slow" / "mempalace", agent_name="agent-a")
    sub = _subsystem(tmp_path, db_url, palace=palace)
    sub._writer_enabled = False
    await sub.ensure_ready()
    await sub.enqueue(thread_id="t", turn_id="1", message_id="m", role="user", content="slow write")
    store = await sub._ensure_outbox()
    writer = MemoryWriter(
        palace=palace,
        outbox=store,
        agent_id="agent-a",
        backend=sub.backend,
        embedding_model=sub.embedding_model,
    )
    flush = asyncio.create_task(writer.flush_once())
    assert await asyncio.to_thread(palace.started.wait, 2)
    assert palace.lock_thread_ident is not None
    assert palace.lock_thread_ident != threading.get_ident()
    stop = asyncio.create_task(writer.stop())
    await asyncio.sleep(0.05)
    assert not stop.done()
    palace.release.set()
    assert await flush == 1
    await stop
    await sub.close()


@pytest.mark.asyncio
async def test_non_sqlite_without_storage_raises(tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, "postgresql://localhost/db")
    with pytest.raises(RuntimeError, match="StorageBackend"):
        await sub.ensure_ready()


@pytest.mark.asyncio
async def test_storage_outbox_used_for_postgres_url(tmp_path: Path) -> None:
    class _Store:
        def __init__(self) -> None:
            self.claimed_for: list[str] = []
            self.inserted: list[dict[str, Any]] = []

        async def insert_pending(self, **kwargs: Any) -> str:
            self.inserted.append(kwargs)
            return "row-1"

        async def claim_batch(self, *, agent_id: str, **kwargs: Any) -> list[Any]:
            del kwargs
            self.claimed_for.append(agent_id)
            return []

        async def mark_committed(self, row_ids: list[str], **kwargs: Any) -> int:
            del row_ids, kwargs
            return 0

        async def mark_retry(self, row_id: str, **kwargs: Any) -> int:
            del row_id, kwargs
            return 0

        async def gc_committed(self, *, days: int = 7) -> int:
            del days
            return 0

        async def pending_depth(self, *, agent_id: str | None = None) -> tuple[int, float]:
            del agent_id
            return 0, 0.0

        async def dead_depth(self, *, agent_id: str | None = None) -> int:
            del agent_id
            return 0

        async def close(self) -> None:
            return

    class _Storage:
        def __init__(self, store: _Store) -> None:
            self._store = store

        def outbox(self) -> _Store:
            return self._store

    store = _Store()
    sub = _subsystem(
        tmp_path,
        "postgresql://localhost/db",
        storage=_Storage(store),
    )
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t", turn_id="1", message_id="m", role="user", content="via storage"
    )
    await sub.drain_writer(timeout_s=1)
    assert store.inserted
    assert store.inserted[0]["agent_id"] == "agent-a"
    assert store.claimed_for == ["agent-a"]
    await sub.close()


def test_cloud_memory_uri_is_rejected() -> None:
    with pytest.raises(UnsupportedMemoryURI, match="gcs://"):
        palace_path_from_uri("gcs://bucket/prefix")
    with pytest.raises(UnsupportedMemoryURI, match="s3://"):
        palace_path_from_uri("s3://bucket/prefix")
    with pytest.raises(UnsupportedMemoryURI):
        palace_path_from_uri("gs://bucket/prefix")


def test_legacy_notes_import_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    (root / "episodic").mkdir(parents=True)
    (root / "episodic" / "cat.md").write_text(
        "---\ntype: episodic\nstatus: active\n---\nRemember the cat.\n",
        encoding="utf-8",
    )
    (root / "INDEX.md").write_text("# Index\n- cat\n", encoding="utf-8")
    palace = InMemoryPalace(root / "mempalace", agent_name="agent-a")
    first = import_legacy_notes(palace, agent_id="agent-a")
    assert first >= 2
    second = import_legacy_notes(palace, agent_id="agent-a")
    assert second == 0
    contents = [d.content for d in palace._drawers.values()]
    assert any("Remember the cat" in c for c in contents)
    assert any("# Index" in c for c in contents)


def test_migrate_memory_uri_writes_bak(tmp_path: Path) -> None:
    yaml_path = tmp_path / "monkeybot.yaml"
    yaml_path.write_text("memory:\n  memory_storage_uri: local://./memory\n", encoding="utf-8")
    assert migrate_memory_uri_in_yaml(yaml_path) is True
    text = yaml_path.read_text(encoding="utf-8")
    assert "local://./memory/mempalace" in text
    bak = yaml_path.with_suffix(yaml_path.suffix + ".bak-pre-mempalace")
    assert bak.is_file()
    assert "local://./memory\n" in bak.read_text(encoding="utf-8")
    assert migrate_memory_uri_in_yaml(yaml_path) is False


def test_memory_enabled_switch_honors_yaml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONKEYBOT_MEMORY_HOOK_ENABLED", raising=False)
    cfg = tmp_path / "monkeybot.yaml"
    cfg.write_text("memory:\n  enabled: false\n", encoding="utf-8")
    assert memory_enabled_from_config(str(cfg)) is False
    monkeypatch.setenv("MONKEYBOT_MEMORY_HOOK_ENABLED", "1")
    assert memory_enabled_from_config(str(cfg)) is True
    monkeypatch.setenv("MONKEYBOT_MEMORY_HOOK_ENABLED", "0")
    cfg.write_text("memory:\n  enabled: true\n", encoding="utf-8")
    assert memory_enabled_from_config(str(cfg)) is False
    monkeypatch.delenv("MONKEYBOT_MEMORY_HOOK_ENABLED", raising=False)
    cfg.write_text("memory_hook:\n  enabled: false\n", encoding="utf-8")
    assert memory_enabled_from_config(str(cfg)) is False
    cfg.write_text("memory:\n  enabled: true\nmemory_hook:\n  enabled: false\n", encoding="utf-8")
    assert memory_enabled_from_config(str(cfg)) is True


@pytest.mark.asyncio
async def test_transient_errors_retry_indefinitely(db_url: str) -> None:
    conn = await open_connection(db_url)
    await ensure_outbox_schema(conn)
    row_id = await insert_pending(
        conn,
        agent_id="a",
        thread_id="t",
        turn_id="turn",
        message_id="m1",
        role="user",
        content="hi",
        workspace_id=None,
        wing="main",
        room="conversation",
    )
    assert row_id is not None
    assert not is_permanent_error("TimeoutError")
    assert is_permanent_error("ValueError")
    assert is_permanent_error("PermanentMemoryError")
    await mark_retry(conn, row_id, error_class="TimeoutError", attempts=99)
    cur = await conn.execute("SELECT status FROM memory_outbox WHERE id = ?", (row_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert str(row[0]) == STATUS_PENDING
    await mark_retry(conn, row_id, error_class="ValueError", attempts=1)
    cur = await conn.execute("SELECT status FROM memory_outbox WHERE id = ?", (row_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert str(row[0]) == STATUS_DEAD
    await conn.close()


@pytest.mark.asyncio
async def test_memory_sqlite_requires_shared_store(tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, "sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="shared StorageBackend"):
        await sub.ensure_ready()


@pytest.mark.asyncio
async def test_in_memory_sqlite_drains_when_storage_is_shared(tmp_path: Path) -> None:
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        sub = _subsystem(tmp_path, "sqlite:///:memory:", storage=backend)
        await sub.ensure_ready()
        await sub.enqueue(
            thread_id="t", turn_id="1", message_id="m", role="user", content="shared memory"
        )
        await sub.drain_writer(timeout_s=2)
        drawers = await sub.recall(wing="main", room="conversation", thread_id="t")
        assert drawers and "shared memory" in drawers[0].content
        await sub.close()
    finally:
        await backend.close()


def test_recall_orders_by_recency_before_limit(tmp_path: Path) -> None:
    palace = InMemoryPalace(tmp_path / "recency", agent_name="agent-a")
    for i in range(90):
        palace.upsert_drawer(
            f"d{i}",
            f"msg-{i}",
            {
                "wing": "main",
                "room": "conversation",
                "filed_at": f"{i:04d}",
                "source_timestamp": f"{i:04d}",
            },
        )
    got = palace.recall(wing="main", room="conversation", n_results=10)
    assert [d.content for d in got] == [f"msg-{i}" for i in range(89, 79, -1)]


def test_chroma_adapter_ranks_all_metadatas_before_limit(tmp_path: Path) -> None:
    adapter = MemPalaceAdapter(tmp_path / "chroma-palace", agent_name="agent-a")
    gets: list[dict[str, Any]] = []

    class _Col:
        def get(self, **kwargs: Any) -> dict[str, Any]:
            gets.append(kwargs)
            if kwargs.get("ids"):
                ids = list(kwargs["ids"])
                return {
                    "ids": ids,
                    "documents": [f"doc-{i}" for i in ids],
                    "metadatas": [
                        {"wing": "main", "room": "conversation", "filed_at": i} for i in ids
                    ],
                }
            ids = [f"d{i}" for i in range(90)]
            metas = [
                {
                    "wing": "main",
                    "room": "conversation",
                    "filed_at": f"{i:04d}",
                    "source_timestamp": f"{i:04d}",
                }
                for i in range(90)
            ]
            return {"ids": ids, "metadatas": metas}

    adapter._collection = lambda create=True: _Col()  # type: ignore[method-assign]
    got = adapter.recall(wing="main", room="conversation", n_results=10)
    assert "limit" not in gets[0]
    assert [d.drawer_id for d in got] == [f"d{i}" for i in range(89, 79, -1)]


def test_compat_exports() -> None:
    from monkeybot.core.memory import (
        INDEX_FILENAME,
        IntegrityResult,
        MemoryIntegrityChecker,
        MemoryPromotionError,
        async_load_index,
        async_promote_to_memory,
        async_search_memory_files,
    )

    assert INDEX_FILENAME == "INDEX.md"
    assert IntegrityResult(ok=True).ok is True
    assert MemoryIntegrityChecker is not None
    assert MemoryPromotionError is not None
    assert callable(async_load_index)
    assert callable(async_promote_to_memory)
    assert callable(async_search_memory_files)


@pytest.mark.asyncio
async def test_compat_async_shims() -> None:
    from monkeybot.core.memory import (
        MemoryPromotionError,
        async_load_index,
        async_promote_to_memory,
        async_search_memory_files,
    )

    assert await async_load_index() == []
    assert await async_search_memory_files("q") == []
    with pytest.raises(MemoryPromotionError):
        await async_promote_to_memory("run", Path("x.md"))


def test_legacy_module_import_paths() -> None:
    from monkeybot.core.memory.graph import MemoryGraph, MemoryGraphStore
    from monkeybot.core.memory.integrity import IntegrityResult, MemoryIntegrityChecker
    from monkeybot.core.memory.storage_ops import INDEX_FILENAME, async_load_index

    assert INDEX_FILENAME == "INDEX.md"
    assert IntegrityResult(ok=True).ok is True
    assert MemoryIntegrityChecker is not None
    assert MemoryGraphStore is not None
    assert MemoryGraph() is not None
    assert callable(async_load_index)


@pytest.mark.asyncio
async def test_export_graph_and_note_body_from_drawers(db_url: str, tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t1", turn_id="1", message_id="m1", role="user", content="graph secret"
    )
    await sub.drain_writer(timeout_s=2)
    payload = await sub.export_graph()
    assert payload["nodes"]
    assert payload["nodes"][0]["status"] == "active"
    drawer_id = payload["nodes"][0]["id"]
    note = await sub.search_files("", path=drawer_id)
    assert note["hits"][0]["body"] == "graph secret"
    await sub.close()


@pytest.mark.asyncio
async def test_append_without_memory_columns_when_schema_is_legacy(tmp_path: Path) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.persistence.history import SQLiteHistoryStore
    from monkeybot.core.types.content_blocks import Text

    conn = await open_connection(f"sqlite:///{tmp_path / 'legacy.db'}")
    await conn.execute(
        """
        CREATE TABLE conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    await conn.commit()
    history = SQLiteHistoryStore(conn)
    await history.append(
        "t",
        Message(role="user", content=[Text(text="hi")]),
        turn_id="turn",
        message_id="msg",
    )
    rows = await history.load("t")
    assert len(rows) == 1
    await conn.close()


@pytest.mark.asyncio
async def test_concurrent_append_with_outbox_does_not_nest_transactions(
    tmp_path: Path,
) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.types.content_blocks import Text

    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'conc.db'}")
    await backend.open()
    try:
        history = backend.history()
        msg = Message(role="user", content=[Text(text="hi")])

        async def one(i: int) -> None:
            await history.append_with_outbox(
                f"t{i}",
                msg,
                turn_id=str(i),
                message_id=str(i),
                outbox={
                    "agent_id": "agent-a",
                    "thread_id": f"t{i}",
                    "turn_id": str(i),
                    "message_id": str(i),
                    "role": "user",
                    "content": "hi",
                    "workspace_id": None,
                    "wing": "main",
                    "room": "conversation",
                },
            )

        await asyncio.gather(*[one(i) for i in range(8)])
        loaded = await history.load("t0")
        assert loaded
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_writer_stop_times_out_on_hung_embedder(db_url: str, tmp_path: Path) -> None:
    from monkeybot.core.memory.writer import MemoryWriter

    palace = _SlowPalace(tmp_path / "hung" / "mempalace", agent_name="agent-a")
    sub = _subsystem(tmp_path, db_url, palace=palace)
    sub._writer_enabled = False
    await sub.ensure_ready()
    await sub.enqueue(thread_id="t", turn_id="1", message_id="m", role="user", content="hung write")
    store = await sub._ensure_outbox()
    writer = MemoryWriter(
        palace=palace,
        outbox=store,
        agent_id="agent-a",
        backend=sub.backend,
        embedding_model=sub.embedding_model,
    )
    writer.start()
    writer.wake()
    assert await asyncio.to_thread(palace.started.wait, 2)
    await asyncio.wait_for(writer.stop(timeout_s=0.2), timeout=1.5)
    palace.release.set()
    await sub.close()


@pytest.mark.asyncio
async def test_history_only_adapter_does_not_receive_memory_kwargs() -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.memory.ingest import persist_message
    from monkeybot.core.types.content_blocks import Text

    seen: list[tuple[object, ...]] = []

    class _LegacyHistory:
        async def append(self, thread_id: str, message: Message) -> None:
            seen.append((thread_id, message))

        async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
            del thread_id, limit
            return []

        async def reset(self, thread_id: str, messages: list[Message]) -> None:
            del thread_id, messages

        async def list_threads(self, limit: int = 50) -> list[object]:
            del limit
            return []

    history = _LegacyHistory()
    await persist_message(
        history,  # type: ignore[arg-type]
        Message(role="user", content=[Text(text="hi")]),
        thread_id="t",
        turn_id="turn",
        memory=None,
        ingest=False,
        message_id="m",
    )
    assert seen and seen[0][0] == "t"


@pytest.mark.asyncio
async def test_outbox_failure_does_not_fail_history_commit(tmp_path: Path) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.memory.ingest import persist_message
    from monkeybot.core.types.content_blocks import Text

    class _History:
        def __init__(self) -> None:
            self.rows: list[Message] = []

        async def append(self, thread_id: str, message: Message, **kwargs: object) -> None:
            del thread_id, kwargs
            self.rows.append(message)

        async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
            del thread_id, limit
            return list(self.rows)

        async def reset(self, thread_id: str, messages: list[Message]) -> None:
            del thread_id
            self.rows = list(messages)

        async def list_threads(self, limit: int = 50) -> list[object]:
            del limit
            return []

    class _Memory:
        ingest_enabled = True
        backend = "chroma"

        def outbox_spec(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        async def enqueue(self, **kwargs: object) -> str:
            del kwargs
            raise RuntimeError("outbox down")

        def wake_writer(self) -> None:
            return

    history = _History()
    await persist_message(
        history,  # type: ignore[arg-type]
        Message(role="user", content=[Text(text="keep me")]),
        thread_id="t",
        turn_id="turn",
        memory=_Memory(),
        ingest=True,
        message_id="m",
    )
    assert history.rows


@pytest.mark.asyncio
async def test_usage_cannot_commit_open_history_outbox_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.llm.usage import Usage
    from monkeybot.core.memory import outbox as outbox_mod
    from monkeybot.core.types.content_blocks import Text

    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'tx.db'}")
    await backend.open()
    try:
        history = backend.history()
        usage = backend.usage()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_insert(conn: object, **kwargs: object) -> str | None:
            del conn, kwargs
            entered.set()
            await release.wait()
            raise RuntimeError("outbox boom")

        monkeypatch.setattr(outbox_mod, "insert_pending", gated_insert)
        msg = Message(role="user", content=[Text(text="hi")])

        async def do_append() -> None:
            with pytest.raises(RuntimeError, match="outbox boom"):
                await history.append_with_outbox(
                    "t",
                    msg,
                    turn_id="1",
                    message_id="m1",
                    outbox={
                        "agent_id": "agent-a",
                        "thread_id": "t",
                        "turn_id": "1",
                        "message_id": "m1",
                        "role": "user",
                        "content": "hi",
                        "workspace_id": None,
                        "wing": "main",
                        "room": "conversation",
                    },
                )

        append_task = asyncio.create_task(do_append())
        await entered.wait()
        usage_task = asyncio.create_task(
            usage.record("t", "model", Usage(input_tokens=1, output_tokens=1))
        )
        await asyncio.sleep(0.05)
        assert not usage_task.done()
        release.set()
        await append_task
        await usage_task
        assert await history.load("t") == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_managed_schema_upgrade_from_documented_ddl(tmp_path: Path) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.memory.ingest import persist_message
    from monkeybot.core.types.content_blocks import Text

    db_path = tmp_path / "pre.db"
    conn = await open_connection(f"sqlite:///{db_path}")
    await conn.execute(
        """
        CREATE TABLE conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    await conn.commit()
    await conn.close()

    backend = SQLiteStorageBackend(f"sqlite:///{db_path}")
    await backend.open(run_schema=False)
    try:
        sql_path = Path(__file__).resolve().parents[3] / "docs" / "migrations" / "memory-outbox.sql"
        sql = sql_path.read_text(encoding="utf-8")
        cleaned: list[str] = []
        for line in sql.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            cleaned.append(stripped)
        for stmt in "\n".join(cleaned).split(";"):
            stmt = stmt.strip()
            if stmt:
                await backend._conn.execute(stmt)  # type: ignore[union-attr]
        await backend._conn.commit()  # type: ignore[union-attr]

        palace = InMemoryPalace(tmp_path / "palace", agent_name="agent-a")
        sub = MemorySubsystem(
            memory_uri=f"local://{palace.palace_path}",
            db_url=f"sqlite:///{db_path}",
            agent_id="agent-a",
            palace=palace,
            storage=backend,
        )
        await sub.ensure_ready()
        await persist_message(
            backend.history(),
            Message(role="user", content=[Text(text="upgraded")]),
            thread_id="t",
            turn_id="1",
            memory=sub,
            ingest=True,
            message_id="m-up",
        )
        await sub.drain_writer(timeout_s=2)
        drawers = await sub.recall(wing="main", room="conversation", thread_id="t")
        assert any("upgraded" in d.content for d in drawers)
        await sub.close()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_ambiguous_commit_does_not_duplicate_history() -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.memory.ingest import persist_message
    from monkeybot.core.types.content_blocks import Text

    class _History:
        def __init__(self) -> None:
            self.rows: list[Message] = []

        async def append_with_outbox(
            self, thread_id: str, message: Message, **kwargs: object
        ) -> None:
            del thread_id, kwargs
            self.rows.append(message)
            raise TimeoutError("ack lost")

        async def append(self, thread_id: str, message: Message, **kwargs: object) -> None:
            del thread_id, kwargs
            self.rows.append(message)

        async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
            del thread_id, limit
            return list(self.rows)

        async def reset(self, thread_id: str, messages: list[Message]) -> None:
            del thread_id
            self.rows = list(messages)

        async def list_threads(self, limit: int = 50) -> list[object]:
            del limit
            return []

    history = _History()
    await persist_message(
        history,  # type: ignore[arg-type]
        Message(role="user", content=[Text(text="once")]),
        thread_id="t",
        turn_id="turn",
        memory=MagicMock(ingest_enabled=True, backend="chroma"),
        ingest=True,
        message_id="m",
    )
    assert len(history.rows) == 1


@pytest.mark.asyncio
async def test_history_append_is_idempotent_on_message_id(tmp_path: Path) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.types.content_blocks import Text

    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'idemp.db'}")
    await backend.open()
    try:
        history = backend.history()
        msg = Message(role="user", content=[Text(text="once")])
        await history.append("t", msg, turn_id="1", message_id="same")
        await history.append("t", msg, turn_id="1", message_id="same")
        loaded = await history.load("t")
        assert len(loaded) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_lease_owner_fences_commit_and_retry(db_url: str) -> None:
    from monkeybot.core.memory.outbox import SqliteOutboxStore, claim_batch, mark_committed

    conn = await open_connection(db_url)
    await ensure_outbox_schema(conn)
    store = SqliteOutboxStore(conn, owns_connection=True)
    row_id = await store.insert_pending(
        agent_id="a",
        thread_id="t",
        turn_id="1",
        message_id="m",
        role="user",
        content="hi",
        workspace_id=None,
        wing="main",
        room="conversation",
        palace_id="p1",
    )
    assert row_id is not None
    claimed = await claim_batch(conn, agent_id="a", lease_owner="owner-a", palace_id="p1")
    assert claimed
    n = await mark_committed(conn, [claimed[0].id], lease_owner="owner-b")
    assert n == 0
    cur = await conn.execute("SELECT status FROM memory_outbox WHERE id = ?", (claimed[0].id,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert str(row[0]) == "processing"
    n = await mark_committed(conn, [claimed[0].id], lease_owner="owner-a")
    assert n == 1
    await store.close()


@pytest.mark.asyncio
async def test_claims_are_partitioned_by_palace_id(db_url: str) -> None:
    from monkeybot.core.memory.outbox import SqliteOutboxStore, claim_batch

    conn = await open_connection(db_url)
    await ensure_outbox_schema(conn)
    store = SqliteOutboxStore(conn, owns_connection=True)
    await store.insert_pending(
        agent_id="a",
        thread_id="t",
        turn_id="1",
        message_id="m1",
        role="user",
        content="one",
        workspace_id=None,
        wing="main",
        room="conversation",
        palace_id="palace-a",
    )
    await store.insert_pending(
        agent_id="a",
        thread_id="t",
        turn_id="1",
        message_id="m2",
        role="user",
        content="two",
        workspace_id=None,
        wing="main",
        room="conversation",
        palace_id="palace-b",
    )
    a_rows = await claim_batch(conn, agent_id="a", lease_owner="w1", palace_id="palace-a")
    assert len(a_rows) == 1
    assert a_rows[0].message_id == "m1"
    b_rows = await claim_batch(conn, agent_id="a", lease_owner="w2", palace_id="palace-b")
    assert len(b_rows) == 1
    assert b_rows[0].message_id == "m2"
    await store.close()


def test_firestore_outbox_collection_id_is_stable() -> None:
    pytest.importorskip("google.cloud.firestore")
    from monkeybot.core.persistence.firestore import _memory_outbox_collection

    class _Ref:
        def __init__(self, name: str) -> None:
            self.path = name

        def document(self, name: str) -> _Ref:
            return _Ref(f"{self.path}/{name}")

        def collection(self, name: str) -> _Ref:
            return _Ref(f"{self.path}/{name}")

    class _Client:
        def collection(self, name: str) -> _Ref:
            return _Ref(name)

    col = _memory_outbox_collection(_Client(), "mb")  # type: ignore[arg-type]
    assert col.path == "mb/outbox/memory_outbox"
    col_default = _memory_outbox_collection(_Client(), "")  # type: ignore[arg-type]
    assert col_default.path == "_default/outbox/memory_outbox"


@pytest.mark.asyncio
async def test_ephemeral_palace_rejected_for_postgres_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONKEYBOT_MEMORY_ALLOW_EPHEMERAL", raising=False)

    class PostgresStorageBackend:
        def outbox(self) -> object:
            raise AssertionError("should not open outbox")

    palace = InMemoryPalace(tmp_path / "ephemeral", agent_name="agent-a")
    sub = MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url="postgresql://localhost/db",
        agent_id="agent-a",
        palace=palace,
        storage=PostgresStorageBackend(),
    )
    with pytest.raises(RuntimeError, match="temp directory"):
        await sub.ensure_ready()
