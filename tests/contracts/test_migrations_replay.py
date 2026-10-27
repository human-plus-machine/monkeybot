"""Schema replay and startup migrations (T1-STORE-20 … 25).

Legacy fixtures and the schema commit each was built from:

* ``pre_row_id`` — b4378f48eb50969d5d1900ba071ab7a2271a8457
* ``pre_agent_scope`` — 16a9f637e343a7c99efe5b567c69862abd95d1f9
* ``pre_usage_cache_cols`` — 3d1e4f488c6bb038928fdcbc8451708ca94a2cb0
  (parent of df9d4212eb3badbdedb0def5f44d95b087be9f19)
* ``pre_branch_pk`` — 164b76cdc3708ad573e993cb24aa725e2bb13cca

``sqlite.py`` at 4f48fe13c06522c1449cd7be9bba107996c6f51a, 3d973e99fe8a311b0621b0f861eec9f3f17bb393,
and 164b76cdc3708ad573e993cb24aa725e2bb13cca already has
``PRIMARY KEY (agent_scope, session_id, branch_id)``. 164b76cd is the last of those whose tree
still records the previous DDL, in ``tests/core/test_session_branches_migration.py``:
``PRIMARY KEY (session_id, branch_id)`` with ``fork_row_index`` and ``fork_fingerprint``.
The ``pre_branch_pk`` fixture uses that DDL.
"""

from __future__ import annotations

import fcntl
import importlib.util
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.config.settings import auto_schema_enabled_from_config
from monkeybot.core.memory.migrate_embedding import migrate_agent_embedding
from monkeybot.core.memory.migrate_layout import migrate_host_memory_volume
from monkeybot.core.memory.palace import EMBEDDER_IDENTITY_FILE
from monkeybot.core.memory.subsystem import MemoryConfigurationError, MemorySubsystem
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import SCHEMA_DDLS, apply_schema, open_connection
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

_LEGACY_DIR = Path(__file__).parent / "fixtures" / "legacy_db"
_LEGACY_NAMES = (
    "pre_row_id",
    "pre_agent_scope",
    "pre_branch_pk",
    "pre_usage_cache_cols",
)
_THREAD = "legacy-thread"


def _declared_columns() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for ddl in SCHEMA_DDLS:
        match = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*)\)", ddl, re.S)
        if match is None:
            continue
        body = re.sub(r"PRIMARY KEY\s*\([^)]*\)", "", match.group(2), flags=re.I)
        columns: set[str] = set()
        for line in body.split(","):
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0].upper() in {"PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN"}:
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parts[0]):
                columns.add(parts[0])
        found[match.group(1)] = columns
    return found


async def _counts(conn: Any) -> dict[str, int]:
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    names = [str(row[0]) for row in await cursor.fetchall()]
    await cursor.close()
    counts: dict[str, int] = {}
    for name in names:
        cursor = await conn.execute(f"SELECT COUNT(*) FROM {name}")
        row = await cursor.fetchone()
        await cursor.close()
        counts[name] = int(row[0]) if row else 0
    return counts


async def _column_names(conn: Any, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return {str(row[1]) for row in rows}


def _schema_snapshot(path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [(str(kind), str(name), " ".join(str(sql).split())) for kind, name, sql in rows]


def _create_schema_main() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "create_schema.py"
    spec = importlib.util.spec_from_file_location("monkeybot_create_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def _write_agent(root: Path, model: str, *, identity: str | None) -> None:
    config = root / "monkeybot_config"
    config.mkdir(parents=True)
    (config / "monkeybot.yaml").write_text(
        "paths:\n"
        "  memory_storage_uri: local://./memory/mempalace\n"
        "memory:\n"
        f"  embedding_model: {model}\n",
        encoding="utf-8",
    )
    palace = root / "memory" / "mempalace"
    palace.mkdir(parents=True)
    (palace / "chroma.sqlite3").write_bytes(b"index")
    if identity is not None:
        (palace / EMBEDDER_IDENTITY_FILE).write_text(identity + "\n", encoding="utf-8")


class _Palace:
    def __init__(self, path: Path) -> None:
        self.palace_path = path
        self.backend = "chroma"

    def ensure_ready(self) -> None:
        raise AssertionError("palace warm ran before the outbox check")


@pytest.mark.case("T1-STORE-20")
@pytest.mark.parametrize("name", _LEGACY_NAMES)
async def test_legacy_db_apply_schema(tmp_path: Path, name: str) -> None:
    dest = tmp_path / f"{name}.sqlite"
    dest.write_bytes((_LEGACY_DIR / f"{name}.sqlite").read_bytes())
    conn = await open_connection(f"sqlite:///{dest}")
    try:
        before = await _counts(conn)
        await apply_schema(conn)
        after = await _counts(conn)
        for table, count in before.items():
            assert after.get(table) == count
        for table, columns in _declared_columns().items():
            assert columns <= await _column_names(conn, table)
        loaded = await SQLiteHistoryStore(conn, agent_scope="").load(_THREAD)
        assert len(loaded) == 1
        assert loaded[0].row_id is not None and loaded[0].row_id.startswith("legacy:")
    finally:
        await conn.close()


@pytest.mark.case("T1-STORE-21")
async def test_create_schema_matches_apply_schema(tmp_path: Path) -> None:
    applied = tmp_path / "apply.sqlite"
    conn = await open_connection(f"sqlite:///{applied}")
    try:
        await apply_schema(conn)
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await conn.close()
    scripted = tmp_path / "script.sqlite"
    assert _create_schema_main()(["--db", str(scripted)]) == 0
    assert _schema_snapshot(applied) == _schema_snapshot(scripted)


@pytest.mark.case("T1-STORE-22")
async def test_auto_schema_false(tmp_path: Path) -> None:
    config = tmp_path / "monkeybot.yaml"
    config.write_text("paths:\n  auto_schema: false\n", encoding="utf-8")
    assert auto_schema_enabled_from_config(str(config)) is False
    db_path = tmp_path / "noschema.sqlite"
    backend = SQLiteStorageBackend(f"sqlite:///{db_path}")
    await backend.open(run_schema=auto_schema_enabled_from_config(str(config)))
    try:
        probed = sqlite3.connect(db_path)
        try:
            missing = probed.execute(
                "SELECT name FROM sqlite_master WHERE name = 'memory_outbox'"
            ).fetchone()
        finally:
            probed.close()
        assert missing is None
        subsystem = MemorySubsystem(
            memory_uri=f"local://{tmp_path / 'memory'}",
            db_url=f"sqlite:///{db_path}",
            agent_id="agent-a",
            palace=_Palace(tmp_path / "palace"),
            storage=backend,
            writer_enabled=False,
            ingest_enabled=False,
        )
        with pytest.raises(MemoryConfigurationError, match="memory_outbox"):
            await subsystem.ensure_ready()
    finally:
        await backend.close()


@pytest.mark.case("T1-STORE-23")
def test_embedding_migrate_lock(tmp_path: Path) -> None:
    gemma = tmp_path / "gemma"
    _write_agent(gemma, "embeddinggemma", identity="embeddinggemma")
    assert migrate_agent_embedding(gemma)["action"] == "noop"
    identity = gemma / "memory" / "mempalace" / EMBEDDER_IDENTITY_FILE
    assert identity.read_text(encoding="utf-8").strip() == "embeddinggemma"

    agent = tmp_path / "legacy"
    _write_agent(agent, "embeddinggemma-300m", identity="embeddinggemma-300m")
    palace = (agent / "memory" / "mempalace").resolve()
    lock_path = palace / ".embedding-migrate.lock"
    handle = lock_path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = migrate_agent_embedding(agent)
        except BaseException as exc:  # noqa: BLE001 — surface the migrator's failure
            box["error"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(0.5)
    returned = not worker.is_alive()
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    worker.join(2)
    assert returned, "second migrator blocked instead of skipping while the lock was held"
    assert "error" not in box
    result = box["result"]
    assert result["action"] != "migrated"
    yaml_text = (agent / "monkeybot_config" / "monkeybot.yaml").read_text(encoding="utf-8")
    assert "embeddinggemma-300m" in yaml_text


@pytest.mark.case("T1-STORE-24")
def test_layout_migrate_nonempty_dest(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    dest = tmp_path / "dest"
    legacy.mkdir()
    dest.mkdir()
    (legacy / "keep.txt").write_text("old", encoding="utf-8")
    (dest / "stay.txt").write_text("new", encoding="utf-8")
    result = migrate_host_memory_volume(legacy, dest)
    assert result["action"] == "skipped_dest_not_empty"
    assert (legacy / "keep.txt").read_text(encoding="utf-8") == "old"
    assert (dest / "stay.txt").read_text(encoding="utf-8") == "new"
    assert not (dest / "keep.txt").exists()


@pytest.mark.case("T1-STORE-25")
@pytest.mark.needs_firestore
async def test_firestore_summary_doc_migration() -> None:
    from monkeybot.core.llm.provider import Message
    from testkit.backends import open_backend

    scope = "scope-a"
    thread = "legacy-thread"
    backend = await open_backend(
        "firestore",
        agent_scope=scope,
        prefix=f"p{uuid.uuid4().hex[:12]}",
    )
    try:
        from monkeybot.core.persistence.firestore import firestore_summary_doc_id

        history = backend.history()
        collection = history._client.collection(history._threads_collection)
        await collection.document(thread).set(
            {
                "thread_id": thread,
                "last_message_at": 1,
                "message_count": 1,
                "last_content": '[{"type":"text","text":"old"}]',
                "agent_scope": scope,
            }
        )
        await history.append(thread, Message.text("user", "next"))
        old = await collection.document(thread).get()
        scoped = await collection.document(firestore_summary_doc_id(scope, thread)).get()
        assert old.exists is False
        assert scoped.exists is True
    finally:
        await backend.close()
