"""Branch store and branch-related history contract, run against every backend.

SQLite always runs. Postgres runs when ``MONKEYBOT_TEST_POSTGRES_URL`` is set;
Firestore runs when ``FIRESTORE_EMULATOR_HOST`` is set.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.branches import ROOT_BRANCH_ID, BranchRecord, branch_thread_id
from monkeybot.core.types.content_blocks import Text


async def _open(kind: str, scope: str) -> Any:
    if kind == "sqlite":
        from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

        backend: Any = SQLiteStorageBackend("sqlite:///:memory:", agent_scope=scope)
    elif kind == "postgres":
        url = os.environ.get("MONKEYBOT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("MONKEYBOT_TEST_POSTGRES_URL not set")
        pytest.importorskip("asyncpg")
        from monkeybot.core.persistence.postgres import PostgresStorageBackend

        backend = PostgresStorageBackend(url, agent_scope=scope)
    else:
        if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
            pytest.skip("FIRESTORE_EMULATOR_HOST not set")
        pytest.importorskip("google.cloud.firestore")
        from monkeybot.core.persistence.backends import FirestoreConfig
        from monkeybot.core.persistence.firestore import FirestoreStorageBackend

        backend = FirestoreStorageBackend(
            FirestoreConfig(project="demo-monkeybot", database="(default)", prefix=f"t{scope}"),
            agent_scope=scope,
        )
    await backend.open()
    return backend


@pytest_asyncio.fixture(params=["sqlite", "postgres", "firestore"])
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    opened = await _open(request.param, uuid.uuid4().hex[:8])
    yield opened
    await opened.close()


def _record(session: str, branch_id: str, *, created_at: int = 10) -> BranchRecord:
    return BranchRecord(
        branch_id=branch_id,
        session_id=session,
        thread_id=branch_thread_id(session, branch_id),
        parent_branch_id=ROOT_BRANCH_ID,
        fork_row_id="row-1",
        op="edit",
        created_at=created_at,
        last_active_at=created_at,
        is_active=False,
    )


@pytest.mark.asyncio
async def test_branch_store_contract(backend: Any) -> None:
    store = backend.branches()
    session = f"s-{uuid.uuid4().hex}"
    assert await store.list(session) == []
    assert await store.get_active(session) is None

    first = await store.create(_record(session, "b1"))
    assert first.is_active
    rows = await store.list(session)
    assert [r.branch_id for r in rows] == [ROOT_BRANCH_ID, "b1"]
    root = rows[0]
    assert (root.thread_id, root.is_active, root.created_at) == (session, False, 0)
    assert rows[1].fork_row_id == "row-1" and rows[1].parent_branch_id == ROOT_BRANCH_ID

    await store.create(_record(session, "b2", created_at=20))
    assert [r.branch_id for r in await store.list(session) if r.is_active] == ["b2"]
    assert len(await store.list(session)) == 3

    switched = await store.set_active(session, ROOT_BRANCH_ID)
    assert switched is not None and switched.is_active and switched.last_active_at > 0
    assert (await store.get_active(session)).branch_id == ROOT_BRANCH_ID
    assert await store.set_active(session, "missing") is None
    assert (await store.get_active(session)).branch_id == ROOT_BRANCH_ID
    assert await store.active_non_root([session]) == {}

    await store.set_active(session, "b1")
    before = (await store.get_active(session)).last_active_at
    await asyncio.sleep(0.01)
    await store.touch(session, "b1")
    assert (await store.get_active(session)).last_active_at > before
    found = await store.active_non_root([session, "unknown", session])
    assert list(found) == [session] and found[session].branch_id == "b1"

    deleted = await store.delete_session(session)
    assert {r.branch_id for r in deleted} == {ROOT_BRANCH_ID, "b1", "b2"}
    assert await store.list(session) == []


@pytest.mark.asyncio
async def test_concurrent_creates_leave_one_active(backend: Any) -> None:
    store = backend.branches()
    session = f"s-{uuid.uuid4().hex}"
    # Routes serialize ops with the turn lock; this checks that contention
    # never commits two active branches. Firestore may abort some attempts.
    results = await asyncio.gather(
        *(store.create(_record(session, f"b{i}")) for i in range(4)), return_exceptions=True
    )
    created = [r for r in results if isinstance(r, BranchRecord)]
    assert created
    rows = await store.list(session)
    assert len(rows) == 1 + len(created)
    assert sum(r.is_active for r in rows) == 1
    await store.delete_session(session)


@pytest.mark.asyncio
async def test_history_hides_branch_threads_and_reports_last_row(backend: Any) -> None:
    history = backend.history()
    session = f"s-{uuid.uuid4().hex}"
    branch = branch_thread_id(session, "b1")
    await history.append(session, Message(role="user", content=[Text(text="root")]))
    await history.append(branch, Message(role="user", content=[Text(text="one")]))
    await history.append(branch, Message(role="assistant", content=[Text(text="two")]))

    listed = [t.thread_id for t in await history.list_threads(200)]
    assert session in listed
    assert branch not in listed
    tail = await history.last_row(branch)
    assert tail is not None and tail[0] == 2 and "two" in tail[1]
    assert await history.last_row(f"missing-{uuid.uuid4().hex}") is None
    await history.reset(session, [])
    await history.reset(branch, [])


@pytest.mark.asyncio
async def test_reset_preserves_row_order_and_ids(backend: Any) -> None:
    history = backend.history()
    thread = f"t-{uuid.uuid4().hex}"
    rows = [
        Message(role="user" if i % 2 == 0 else "assistant", content=[Text(text=f"m{i}")])  # type: ignore[arg-type]
        for i in range(12)
    ]
    await history.reset(thread, rows)
    loaded = await history.load(thread)
    assert [m.content[0].text for m in loaded] == [f"m{i}" for i in range(12)]  # type: ignore[union-attr]
    copy = f"t-{uuid.uuid4().hex}"
    await history.reset(copy, loaded[:5])
    assert [m.row_id for m in await history.load(copy)] == [m.row_id for m in loaded[:5]]
    await history.reset(thread, [])
    await history.reset(copy, [])
