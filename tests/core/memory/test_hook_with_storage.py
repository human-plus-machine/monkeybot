"""MemoryHook exercised against :class:`tests.core.memory.fake_workspace_storage.FakeWorkspaceStorage`."""

from __future__ import annotations

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.memory.hook import MemoryHook
from tests.core.memory.fake_workspace_storage import FakeWorkspaceStorage


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
    )


def _payload(event: HookEvent, **kw) -> HookPayload:
    return HookPayload(event=event, thread_id="t1", request_id="r1", ctx=_ctx(), **kw)


@pytest.mark.asyncio
async def test_on_user_message_appends_chat_log() -> None:
    st = FakeWorkspaceStorage()
    hook = MemoryHook(storage=st)
    await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message="Hello"))
    body = st.files["chat_log.md"]
    assert "Hello" in body
    assert body.count("\n- [") == 1


@pytest.mark.asyncio
async def test_second_user_message_appends() -> None:
    st = FakeWorkspaceStorage()
    hook = MemoryHook(storage=st)
    await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message="a"))
    await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message="b"))
    assert st.files["chat_log.md"].count("\n- [") == 2


@pytest.mark.asyncio
async def test_on_post_tool_writes_raw_and_dedup_skips_duplicate() -> None:
    st = FakeWorkspaceStorage()
    hook = MemoryHook(storage=st, dedup_ttl_sec=3600.0)
    p1 = _payload(
        HookEvent.POST_TOOL,
        tool_name="run_command",
        tool_args={"command": "ls"},
        tool_result="out",
    )
    await hook.on_post_tool(p1)
    raw_keys = [k for k in st.files if k.startswith("raw/")]
    assert len(raw_keys) == 1
    first = raw_keys[0]
    await hook.on_post_tool(p1)
    assert len([k for k in st.files if k.startswith("raw/")]) == 1
    assert first in st.files


@pytest.mark.asyncio
async def test_on_pre_turn_injects_memory_lines() -> None:
    st = FakeWorkspaceStorage()
    st.files["semantic/x.md"] = "unique-token-xyz"
    hook = MemoryHook(storage=st)
    payload = _payload(HookEvent.PRE_TURN, user_message="Tell me about unique-token-xyz")
    await hook.on_pre_turn(payload)
    assert payload.inject_memory_lines
