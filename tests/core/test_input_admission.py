"""Tests for steer / follow-up input admission (P1.1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, ToolCall
from monkeybot.core.runtime.events import UserSteered
from monkeybot.core.runtime.input_admission import (
    AdmissionQueueFullError,
    InputAdmission,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry

from tests.core.test_loop import AllowInspector, FakeHistory, _ctx


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


def test_input_admission_steer_fifo() -> None:
    adm = InputAdmission(max_steer=3, max_follow_up=3)
    assert adm.enqueue_steer([Text(text="a")]) == 0
    assert adm.enqueue_steer([Text(text="b")]) == 1
    first = adm.pop_steer()
    assert first is not None and isinstance(first[0], Text) and first[0].text == "a"
    second = adm.pop_steer()
    assert second is not None and isinstance(second[0], Text) and second[0].text == "b"
    assert adm.pop_steer() is None


def test_input_admission_full_raises() -> None:
    adm = InputAdmission(max_steer=1, max_follow_up=1)
    adm.enqueue_steer([Text(text="a")])
    with pytest.raises(AdmissionQueueFullError):
        adm.enqueue_steer([Text(text="b")])


@pytest.mark.asyncio
async def test_steer_injected_after_tool_batch_before_next_provider_call() -> None:
    """Steer enqueued during tool execute lands before the follow-up provider call."""
    admission = InputAdmission()
    seen_user_msgs: list[str] = []

    class SlowThenDoneProvider:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def name(self) -> str:
            return "fake"

        @property
        def supports_streaming(self) -> bool:
            return True

        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            del tools, model, thinking_budget
            self.calls += 1
            # Capture latest user text visible to the provider.
            for m in reversed(list(messages)):
                if m.role == "user" and m.content:
                    block = m.content[0]
                    if isinstance(block, Text):
                        seen_user_msgs.append(block.text)
                        break
            if self.calls == 1:
                yield ToolCall(call_id="c1", name="read_file", args={"path": "x.md"})
                yield Done()
            else:
                yield TextDelta(text="after steer")
                yield Done()

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> int:
            del messages, tools, model, thinking_budget
            return 0

    class SteerOnExecute:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del call, ctx
            admission.enqueue_steer([Text(text="steer mid-tool")])
            await asyncio.sleep(0.01)
            return ToolExecutionResult.ok_text("file ok")

    prov = SlowThenDoneProvider()
    hist = FakeHistory()
    ctx = _ctx()
    ctx = TurnContext(
        **{**ctx.__dict__, "tools": [ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True)]}
    )
    events: list[object] = []
    async for e in run(
        "start",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=SteerOnExecute(),
        max_turns=4,
        input_admission=admission,
    ):
        events.append(e)

    steered = [e for e in events if isinstance(e, UserSteered)]
    assert len(steered) == 1
    assert steered[0].text == "steer mid-tool"
    assert prov.calls == 2
    assert "steer mid-tool" in seen_user_msgs
    assert admission.pop_steer() is None


@pytest.mark.asyncio
async def test_steer_requires_busy_session(registry: SessionRegistry) -> None:
    app = create_app(registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r = await client.post(
            f"/sessions/{sid}/steer",
            json={"message": "nudge"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "SESSION_IDLE"


@pytest.mark.asyncio
async def test_steer_accepted_while_busy(registry: SessionRegistry) -> None:
    hold = asyncio.Event()

    class HoldingLoop:
        async def start_turn(
            self,
            session_id: str,
            request_id: str,
            user_content: list[Text],
        ) -> None:
            _ = (session_id, request_id, user_content)
            await hold.wait()

    app = create_app(loop_port=HoldingLoop(), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r1 = await client.post(
            f"/sessions/{sid}/reply",
            json={"request_id": "a", "message": "one"},
        )
        assert r1.status_code == 200
        r2 = await client.post(
            f"/sessions/{sid}/steer",
            json={"message": "nudge"},
        )
        assert r2.status_code == 202
        body = r2.json()
        assert body["queue"] == "steer"
        assert body["position"] == 0
        assert body["request_id"] == "a"
        bus = registry.get(sid)
        assert bus is not None
        assert bus.admission.steer_depth == 1
        hold.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_queue_drains_only_when_idle(registry: SessionRegistry) -> None:
    started: list[str] = []
    release = asyncio.Event()

    class TrackingLoop:
        async def start_turn(
            self,
            session_id: str,
            request_id: str,
            user_content: list[Text],
        ) -> None:
            _ = (session_id, user_content)
            started.append(request_id)
            if request_id == "a":
                await release.wait()

    app = create_app(loop_port=TrackingLoop(), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r1 = await client.post(
            f"/sessions/{sid}/reply",
            json={"request_id": "a", "message": "one"},
        )
        assert r1.status_code == 200
        await asyncio.sleep(0.02)
        assert started == ["a"]

        r2 = await client.post(
            f"/sessions/{sid}/queue",
            json={"request_id": "b", "message": "two"},
        )
        assert r2.status_code == 202
        assert r2.json()["queue"] == "follow_up"
        await asyncio.sleep(0.02)
        assert started == ["a"], "follow-up must not start while busy"

        release.set()
        for _ in range(50):
            if started == ["a", "b"]:
                break
            await asyncio.sleep(0.02)
        assert started == ["a", "b"]


@pytest.mark.asyncio
async def test_queue_while_idle_starts_immediately(registry: SessionRegistry) -> None:
    started: list[str] = []

    class TrackingLoop:
        async def start_turn(
            self,
            session_id: str,
            request_id: str,
            user_content: list[Text],
        ) -> None:
            _ = (session_id, user_content)
            started.append(request_id)

    app = create_app(loop_port=TrackingLoop(), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r = await client.post(
            f"/sessions/{sid}/queue",
            json={"request_id": "q1", "message": "hello"},
        )
        assert r.status_code == 202
        assert r.json()["position"] == 0
        await asyncio.sleep(0.05)
        assert started == ["q1"]
