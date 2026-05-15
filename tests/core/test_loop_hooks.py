"""Tests for HookManager integration in :mod:`monkeybot.core.loop`.

Verifies that:

* All five hook events fire at the expected lifecycle points.
* ``PRE_TURN.inject_text`` lands in the system prompt for the current turn.
* ``PRE_TURN.inject_memory_lines`` are added to ``ctx.memory_index``.
* ``PRE_TOOL.inject_text`` lands in the **next** system prompt (not the tool result).
* Tool results sent to the provider are unmodified (ground truth).
* Hooks are optional: omitting ``hook_manager`` leaves loop behavior unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from monkeybot.core.content_blocks import Text, ToolResponse
from monkeybot.core.context import TurnContext
from monkeybot.core.events import SystemPromptSnapshot
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.inspector import Decision
from monkeybot.core.loop import run
from monkeybot.core.provider import Done, Message, ProviderEvent, TextDelta, ToolCall, UsageEvent
from monkeybot.core.types_tools import ToolDef


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


class FakeHistory:
    def __init__(self) -> None:
        self.rows: list[Message] = []

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        del thread_id, limit
        return list(self.rows)

    async def append(self, thread_id: str, message: Message) -> None:
        del thread_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.rows = list(messages)


class CapturingProvider:
    """Records the system message text passed in for each ``stream()`` call."""

    def __init__(self, scripted: list[list[ProviderEvent]]) -> None:
        self._scripted = scripted
        self.system_texts: list[str] = []
        self._idx = 0

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
    ) -> AsyncIterator[ProviderEvent]:
        del tools, model
        system = next((m for m in messages if m.role == "system"), None)
        text = ""
        if system is not None:
            text = "".join(b.text for b in system.content if isinstance(b, Text))
        self.system_texts.append(text)
        idx = self._idx
        self._idx += 1
        if idx >= len(self._scripted):
            return
        for ev in self._scripted[idx]:
            yield ev


class RecordingExecutor:
    def __init__(self, result: tuple[str | None, str | None] = ("ok", None)) -> None:
        self.result = result
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
        del ctx
        self.calls.append(call)
        return self.result


class AllowInspector:
    async def check(self, call: object, ctx: object) -> Decision:
        del call, ctx
        return Decision(kind="allow")


@pytest.mark.asyncio
async def test_no_hook_manager_keeps_behavior_unchanged() -> None:
    """Loop must run end-to-end identically when ``hook_manager`` is omitted."""
    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    hist = FakeHistory()
    exe = RecordingExecutor()
    events = [
        e
        async for e in run(
            "hello",
            _ctx(),
            provider=prov,
            history=hist,
            inspectors=[AllowInspector()],
            tool_executor=exe,
            max_turns=2,
        )
    ]
    assert any(getattr(e, "kind", None) == "TurnComplete" for e in events)
    assert prov.system_texts and "<memory>" not in prov.system_texts[0]


@pytest.mark.asyncio
async def test_user_message_pre_turn_and_post_turn_all_fire() -> None:
    """All three fire. USER_MESSAGE + POST_TURN are fire-and-forget so wall-clock
    order vs. PRE_TURN is not guaranteed; only PRE_TURN is awaited synchronously."""
    mgr = HookManager()
    fired: list[HookEvent] = []

    async def record(p: HookPayload) -> None:
        fired.append(p.event)

    for ev in (
        HookEvent.USER_MESSAGE,
        HookEvent.PRE_TURN,
        HookEvent.POST_TURN,
    ):
        mgr.register(ev, record)

    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        "hi",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass

    await asyncio.sleep(0.02)  # let fire-and-forget USER_MESSAGE / POST_TURN drain
    assert set(fired) == {
        HookEvent.USER_MESSAGE,
        HookEvent.PRE_TURN,
        HookEvent.POST_TURN,
    }, f"missing hook event(s): {fired}"


@pytest.mark.asyncio
async def test_pre_turn_fires_before_provider_stream() -> None:
    """PRE_TURN must complete before the first provider.stream call so any
    injection lands in that turn's system prompt."""
    mgr = HookManager()
    pre_turn_seen_n_streams: list[int] = []
    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])

    async def record_n(p: HookPayload) -> None:
        del p
        pre_turn_seen_n_streams.append(len(prov.system_texts))

    mgr.register(HookEvent.PRE_TURN, record_n)

    async for _ in run(
        "hi",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass

    assert pre_turn_seen_n_streams == [0]


@pytest.mark.asyncio
async def test_pre_turn_inject_text_lands_in_system_prompt() -> None:
    mgr = HookManager()

    async def inject(p: HookPayload) -> None:
        p.inject_text = "REMEMBER: user is Karthik."

    mgr.register(HookEvent.PRE_TURN, inject)

    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        "hi",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass

    assert prov.system_texts, "provider.stream was never called"
    assert "<memory>" in prov.system_texts[0]
    assert "REMEMBER: user is Karthik." in prov.system_texts[0]


@pytest.mark.asyncio
async def test_pre_turn_inject_memory_lines_appear_in_context() -> None:
    mgr = HookManager()
    seen_indexes: list[list[str]] = []

    async def inject(p: HookPayload) -> None:
        p.inject_memory_lines = ["- pref: dark mode", "- name: Karthik"]

    async def capture(p: HookPayload) -> None:
        seen_indexes.append(list(p.ctx.memory_index))

    mgr.register(HookEvent.PRE_TURN, inject)
    mgr.register(HookEvent.POST_TURN, capture)

    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        "hi",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass

    await asyncio.sleep(0.02)
    assert seen_indexes == [["- pref: dark mode", "- name: Karthik"]]


@pytest.mark.asyncio
async def test_pre_tool_fires_before_post_tool_and_carries_args() -> None:
    mgr = HookManager()
    fired: list[tuple[HookEvent, str | None, dict[str, Any] | None]] = []

    async def record(p: HookPayload) -> None:
        fired.append((p.event, p.tool_name, p.tool_args))

    mgr.register(HookEvent.PRE_TOOL, record)
    mgr.register(HookEvent.POST_TOOL, record)

    # turn 1: provider asks for run_command; turn 2: provider returns final text
    prov = CapturingProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "ls"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    async for _ in run(
        "go",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(result=("listing", None)),
        max_turns=4,
        hook_manager=mgr,
    ):
        pass

    await asyncio.sleep(0.02)
    pre = [f for f in fired if f[0] == HookEvent.PRE_TOOL]
    post = [f for f in fired if f[0] == HookEvent.POST_TOOL]
    assert len(pre) == 1 and len(post) == 1
    assert pre[0][1] == "run_command" and pre[0][2] == {"command": "ls"}
    assert post[0][1] == "run_command"
    # Confirm order PRE_TOOL precedes POST_TOOL
    assert fired.index((HookEvent.PRE_TOOL, "run_command", {"command": "ls"})) < fired.index(
        (HookEvent.POST_TOOL, "run_command", {"command": "ls"})
    )


@pytest.mark.asyncio
async def test_pre_tool_injection_lands_on_next_system_prompt_only() -> None:
    """PRE_TOOL.inject_text must appear in the NEXT provider call's system prompt,
    not in the tool result, and must clear after one use."""
    mgr = HookManager()

    async def inject(p: HookPayload) -> None:
        p.inject_text = "ABOUT THIS FILE: it uses jose"

    mgr.register(HookEvent.PRE_TOOL, inject)

    prov = CapturingProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "ls"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    exe = RecordingExecutor(result=("listing", None))
    hist = FakeHistory()

    async for _ in run(
        "go",
        _ctx(),
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=4,
        hook_manager=mgr,
    ):
        pass

    assert len(prov.system_texts) == 2
    assert "ABOUT THIS FILE" not in prov.system_texts[0], "must not appear before tool call"
    assert "ABOUT THIS FILE" in prov.system_texts[1], "must appear in next provider call"

    # Tool result in history must be the raw executor output, unmodified.
    tool_response = next(
        (
            b
            for m in hist.rows
            for b in m.content
            if isinstance(b, ToolResponse)
        ),
        None,
    )
    assert tool_response is not None
    body = "".join(t.text for t in tool_response.result if isinstance(t, Text))
    assert body == "listing"
    assert "ABOUT THIS FILE" not in body


@pytest.mark.asyncio
async def test_pre_tool_extra_clears_after_one_use() -> None:
    """A PRE_TOOL injection on turn 1 must NOT leak into turn 3's system prompt."""
    mgr = HookManager()
    call_count = {"n": 0}

    async def inject_once(p: HookPayload) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            p.inject_text = "FIRST-TOOL-MEMORY"

    mgr.register(HookEvent.PRE_TOOL, inject_once)

    prov = CapturingProvider(
        [
            # turn 1: ask for tool
            [ToolCall(call_id="c1", name="run_command", args={"command": "a"}), Done()],
            # turn 2: tool result returned; ask for ANOTHER tool
            [ToolCall(call_id="c2", name="run_command", args={"command": "b"}), Done()],
            # turn 3: final text
            [TextDelta(text="done"), Done()],
        ]
    )
    async for _ in run(
        "go",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(result=("x", None)),
        max_turns=6,
        hook_manager=mgr,
    ):
        pass

    assert len(prov.system_texts) == 3
    assert "FIRST-TOOL-MEMORY" not in prov.system_texts[0]
    assert "FIRST-TOOL-MEMORY" in prov.system_texts[1]
    assert "FIRST-TOOL-MEMORY" not in prov.system_texts[2], "transient extra must clear"


@pytest.mark.asyncio
async def test_post_tool_payload_contains_result_text() -> None:
    mgr = HookManager()
    seen: list[tuple[str | None, str | None]] = []

    async def grab(p: HookPayload) -> None:
        seen.append((p.tool_result, p.tool_error))

    mgr.register(HookEvent.POST_TOOL, grab)

    prov = CapturingProvider(
        [
            [ToolCall(call_id="c1", name="run_command", args={}), Done()],
            [TextDelta(text="ok"), Done()],
        ]
    )
    async for _ in run(
        "go",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(result=("RESULTBODY", None)),
        max_turns=4,
        hook_manager=mgr,
    ):
        pass

    await asyncio.sleep(0.02)
    assert seen == [("RESULTBODY", None)]
