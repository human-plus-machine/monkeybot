"""Unit tests for SessionRegistry + InMemoryCheckpointer."""

from __future__ import annotations

import pytest

from src.core.harness.checkpointer import InMemoryCheckpointer
from src.core.harness.control import SessionRegistry
from src.core.harness.event_bus import EventBus
from src.core.harness.events import Principal
from src.core.harness.errors import HarnessError


@pytest.mark.asyncio
async def test_inmemory_write_read_list_delete() -> None:
    c = InMemoryCheckpointer()
    ref = await c.write("s1", {"foo": 1}, reason="test")
    read = await c.read("s1")
    assert read == {"foo": 1}
    refs = await c.list("s1")
    assert refs == [ref]
    await c.delete_session("s1")
    assert await c.list("s1") == []


@pytest.mark.asyncio
async def test_session_lifecycle() -> None:
    bus = EventBus(include_default_logger=False)
    reg = SessionRegistry(InMemoryCheckpointer(), bus)
    await reg.register("s1", principal=Principal(kind="user", id="u1"), agent_name="a")
    await reg.pause("s1")
    assert reg.ensure_active.__name__
    await reg.resume("s1")
    reg.ensure_active("s1")
    await reg.revoke("s1", "security incident")
    with pytest.raises(HarnessError):
        reg.ensure_active("s1")


@pytest.mark.asyncio
async def test_rewind_loads_checkpoint() -> None:
    bus = EventBus(include_default_logger=False)
    reg = SessionRegistry(InMemoryCheckpointer(), bus)
    await reg.register("s1", principal=Principal(kind="user", id="u1"), agent_name="a")
    ref = await reg.checkpoint("s1", {"state": 1}, reason="manual")
    restored = await reg.rewind("s1", ref.id)
    assert restored == {"state": 1}
