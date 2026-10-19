"""Tests for HookManager integration in :mod:`monkeybot.core.runtime.loop`.

Verifies that:

* Lifecycle hook events fire at the expected points.
* ``PRE_TURN.inject_text`` lands in the system prompt for the current turn.
* ``PRE_TURN.inject_memory_lines`` are added to ``ctx.memory_index``.
* ``PRE_TOOL.inject_text`` lands in the **next** system prompt (not the tool result).
* Tool results sent to the provider are unmodified (ground truth).
* ``TOOL_DEFINITION`` can filter tools before the provider call.
* ``BEFORE_PROVIDER_REQUEST`` can rewrite provider messages.
* ``AFTER_PROVIDER_RESPONSE`` fires with assistant text / usage (fire-and-forget).
* Hooks are optional: omitting ``hook_manager`` leaves loop behavior unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.testing.mocks_provider import fake_provider_prompt_tokens
from monkeybot.core.tools.inspector import Decision
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse
from monkeybot.core.types.types_tools import ToolDef


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

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del thread_id, turn_id, message_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.rows = list(messages)


class CapturingProvider:
    """Records the system message text passed in for each ``stream()`` call."""

    def __init__(self, scripted: list[list[ProviderEvent]]) -> None:
        self._scripted = scripted
        self.system_texts: list[str] = []
        self.tool_names: list[list[str]] = []
        self.message_snapshots: list[list[Message]] = []
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
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del model, thinking_budget
        system = next((m for m in messages if m.role == "system"), None)
        text = ""
        if system is not None:
            text = "".join(b.text for b in system.content if isinstance(b, Text))
        self.system_texts.append(text)
        self.tool_names.append([t.name for t in tools])
        self.message_snapshots.append(list(messages))
        idx = self._idx
        self._idx += 1
        if idx >= len(self._scripted):
            return
        for ev in self._scripted[idx]:
            yield ev

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        del model, thinking_budget
        return fake_provider_prompt_tokens(messages, tools)


def _tool_result(
    result: ToolExecutionResult | tuple[str | None, str | None],
) -> ToolExecutionResult:
    if isinstance(result, ToolExecutionResult):
        return result
    text, err = result
    if err is not None:
        return ToolExecutionResult.err(err)
    return ToolExecutionResult.ok_text(text or "")


class RecordingExecutor:
    def __init__(
        self,
        result: ToolExecutionResult | tuple[str | None, str | None] = ("ok", None),
    ) -> None:
        self.result = _tool_result(result)
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
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
    assert prov.system_texts and "## Runtime notes" not in prov.system_texts[0]


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
    assert "## Runtime notes" in prov.system_texts[0]
    assert "REMEMBER: user is Karthik." in prov.system_texts[0]


@pytest.mark.asyncio
async def test_git_context_hook_note_lands_in_runtime_notes_not_the_stable_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.coding.git_context_hook import GitContextHook
    from monkeybot.providers._utils import split_system_prompt_for_cache

    async def fake_note(root: Path, **_: object) -> str:
        return "git: branch feat/x, 2 modified (a.py, b.py)"

    monkeypatch.setattr("monkeybot.core.coding.git_context_hook.git_status_note", fake_note)
    mgr = HookManager()
    GitContextHook().register(mgr)
    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        "hi",
        dataclasses.replace(_ctx(), workspace_root=tmp_path),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass
    stable, volatile = split_system_prompt_for_cache(prov.system_texts[0])
    assert "git: branch feat/x, 2 modified (a.py, b.py)" in prov.system_texts[0]
    assert "git: branch" not in stable
    assert "## Runtime notes" in prov.system_texts[0]


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
        (b for m in hist.rows for b in m.content if isinstance(b, ToolResponse)),
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


@pytest.mark.asyncio
async def test_tool_definition_hook_filters_tools_sent_to_provider() -> None:
    mgr = HookManager()

    async def observe_only(p: HookPayload) -> None:
        assert p.tools is not None
        p.tools = [t for t in p.tools if t.name != "run_command"]

    mgr.register(HookEvent.TOOL_DEFINITION, observe_only)

    ctx = _ctx()
    ctx = dataclasses.replace(
        ctx,
        tools=[
            ToolDef("run_command", "Run shell", {}),
            ToolDef("read_file", "Read a file", {}),
        ],
    )
    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        "hi",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass

    assert prov.tool_names == [["read_file"]]


@pytest.mark.asyncio
async def test_before_provider_request_can_rewrite_messages() -> None:
    mgr = HookManager()

    async def scrub(p: HookPayload) -> None:
        assert p.provider_messages is not None
        rewritten: list[Message] = []
        for m in p.provider_messages:
            if m.role != "user":
                rewritten.append(m)
                continue
            parts: list[ContentBlock] = []
            for b in m.content:
                if isinstance(b, Text):
                    parts.append(Text(text=b.text.replace("SECRET", "[redacted]")))
                else:
                    parts.append(b)
            rewritten.append(Message(role="user", content=parts))
        p.provider_messages = rewritten

    mgr.register(HookEvent.BEFORE_PROVIDER_REQUEST, scrub)

    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        "token SECRET here",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass

    assert prov.message_snapshots
    user = next(m for m in prov.message_snapshots[0] if m.role == "user")
    body = "".join(b.text for b in user.content if isinstance(b, Text))
    assert "SECRET" not in body
    assert "[redacted]" in body


@pytest.mark.asyncio
async def test_after_provider_response_fires_with_usage() -> None:
    mgr = HookManager()
    seen: list[HookPayload] = []

    async def capture(p: HookPayload) -> None:
        seen.append(p)

    mgr.register(HookEvent.AFTER_PROVIDER_RESPONSE, capture)

    prov = CapturingProvider([[TextDelta(text="hello world"), Done()]])
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
    assert len(seen) == 1
    assert seen[0].assistant_text == "hello world"
    assert seen[0].provider_error is None
    assert seen[0].usage is not None
    assert "input_tokens" in seen[0].usage
    assert seen[0].tool_requests == []


@pytest.mark.asyncio
async def test_identical_tool_definition_list_does_not_set_tools_dirty(tmp_path: Path) -> None:
    from monkeybot.core.persistence.transcript import TranscriptWriter

    mgr = HookManager()

    async def echo_tools(p: HookPayload) -> None:
        assert p.tools is not None
        p.tools = list(p.tools)

    mgr.register(HookEvent.TOOL_DEFINITION, echo_tools)
    writer = TranscriptWriter("sess-hook", workspace_root=tmp_path)
    prov = CapturingProvider(
        [
            [ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}), Done()],
            [TextDelta(text="ok"), Done()],
        ]
    )
    async for _ in run(
        "hi",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=5,
        hook_manager=mgr,
        transcript_writer=writer,
    ):
        pass

    import json

    lines = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
    requests = [line for line in lines if line["type"] == "ProviderRequest"]
    assert len(requests) == 2
    assert "tools" in requests[0]
    assert "tools" not in requests[1]


def test_tools_dirty_reason_keeps_the_first_cause() -> None:
    """A hook re-marking MCP-dirtied tools must not relabel the change as its own."""
    from monkeybot.core.runtime.doom_loop import _DoomLoopTracker
    from monkeybot.core.runtime.tool_dispatch import ToolBatchState
    from monkeybot.core.runtime.turn_loop import _mark_tools_dirty, _TurnState

    batch = ToolBatchState(ctx=_ctx(), tools_dirty=False, pre_tool_extra_next=None)
    batch.tools_dirty_reason = "mcp"
    if batch.tools_dirty_reason is None:
        batch.tools_dirty_reason = "loops"
    assert batch.tools_dirty_reason == "mcp"

    state = _TurnState(
        ctx=_ctx(),
        user_text="hi",
        effective_max=5,
        doom_tracker=_DoomLoopTracker(threshold=3),
        tools_dirty_reason=batch.tools_dirty_reason,
    )
    _mark_tools_dirty(state, "hook", [ToolDef("run_command", "Run shell", {})])
    assert state.tools_dirty is True
    assert state.tools_dirty_reason == "mcp"

    state.tools_dirty_reason = None
    _mark_tools_dirty(
        state,
        "hook",
        [ToolDef("run_command", "Run shell", {}), ToolDef("read_file", "Read a file", {})],
    )
    assert state.tools_dirty_reason == "hook", "an unattributed change still records a cause"


# --- Credential broker phase 5.3: outbound scan wired into the turn loop ---


class _MarkerScanner:
    """Reports a hit for any text containing MARKER, mirroring SecretScanner's shape."""

    MARKER = "the-secret-value"

    def scan(self, texts: list[str]) -> list[object]:
        from monkeybot.core.context.secret_egress import Hit

        return [Hit(index=i, kind="secret") for i, t in enumerate(texts) if self.MARKER in t]


@pytest.mark.asyncio
async def test_credential_egress_blocked_redacts_before_next_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret in turn 1's assistant text never reaches turn 2's provider call."""
    from monkeybot.core.runtime import loop_hooks

    monkeypatch.setattr(loop_hooks, "get_scanner", lambda: _MarkerScanner())
    # MONKEYBOT_RUN_ID is grant scope, not execution kind: an interactive chat's
    # gateway spawn sets it to the session key, so its mere presence must not
    # be read as "this is a routine".
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "gateway-session-key")
    monkeypatch.delenv("MONKEYBOT_RUN_KIND", raising=False)

    prov = CapturingProvider(
        [
            [
                TextDelta(text=f"here is {_MarkerScanner.MARKER}"),
                ToolCall(call_id="c1", name="run_command", args={"command": "ls"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    events = [
        e
        async for e in run(
            "go",
            _ctx(),
            provider=prov,
            history=FakeHistory(),
            inspectors=[AllowInspector()],
            tool_executor=RecordingExecutor(result=("listing", None)),
            max_turns=4,
        )
    ]

    assert any(getattr(e, "kind", None) == "CredentialEgressBlocked" for e in events)
    # Chat (MONKEYBOT_RUN_KIND unset/not "routine"): the turn continues to a
    # second provider call, even though MONKEYBOT_RUN_ID is set.
    assert len(prov.message_snapshots) == 2
    assistant_msg = next(m for m in prov.message_snapshots[1] if m.role == "assistant")
    body = "".join(b.text for b in assistant_msg.content if isinstance(b, Text))
    assert _MarkerScanner.MARKER not in body
    assert "[withheld: credential detected]" in body
    assert any(getattr(e, "kind", None) == "TurnComplete" for e in events)


@pytest.mark.asyncio
async def test_credential_egress_blocked_ends_routine_without_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With MONKEYBOT_RUN_KIND=routine, a hit ends the turn instead of calling the provider again."""
    from monkeybot.core.runtime import loop_hooks

    monkeypatch.setattr(loop_hooks, "get_scanner", lambda: _MarkerScanner())
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-123")
    monkeypatch.setenv("MONKEYBOT_RUN_KIND", "routine")

    prov = CapturingProvider(
        [
            [
                TextDelta(text=f"here is {_MarkerScanner.MARKER}"),
                ToolCall(call_id="c1", name="run_command", args={"command": "ls"}),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    events = [
        e
        async for e in run(
            "go",
            _ctx(),
            provider=prov,
            history=FakeHistory(),
            inspectors=[AllowInspector()],
            tool_executor=RecordingExecutor(result=("listing", None)),
            max_turns=4,
        )
    ]

    assert any(getattr(e, "kind", None) == "CredentialEgressBlocked" for e in events)
    assert any(
        getattr(e, "kind", None) == "Error" and "Blocked" in (getattr(e, "error", None) or "")
        for e in events
    )
    assert not any(
        getattr(e, "kind", None) == "Error"
        and "Max turns exceeded" in (getattr(e, "error", None) or "")
        for e in events
    )
    # The second provider call (which would have sent the secret) never happens.
    assert len(prov.message_snapshots) == 1


# --- PRE_FINAL_ANSWER seam ---


async def _run_collect(
    prov: CapturingProvider,
    mgr: HookManager | None,
    *,
    max_turns: int = 6,
    hist: FakeHistory | None = None,
) -> list[Any]:
    return [
        e
        async for e in run(
            "go",
            _ctx(),
            provider=prov,
            history=hist if hist is not None else FakeHistory(),
            inspectors=[AllowInspector()],
            tool_executor=RecordingExecutor(),
            max_turns=max_turns,
            hook_manager=mgr,
        )
    ]


def _deltas(events: list[Any]) -> str:
    return "".join(e.delta for e in events if getattr(e, "kind", None) == "AssistantDelta")


def _gate_events(events: list[Any]) -> list[Any]:
    return [
        e
        for e in events
        if getattr(e, "kind", None) == "HarnessIntervention"
        and e.intervention == "final_answer_gate"
    ]


def _answer_script(*answers: str) -> list[list[ProviderEvent]]:
    return [[TextDelta(text=a), Done()] for a in answers]


@pytest.mark.asyncio
async def test_pre_final_answer_rejection_discards_held_text_and_asks_again() -> None:
    mgr = HookManager()
    seen_candidates: list[str | None] = []

    async def hold(p: HookPayload) -> None:
        p.hold_assistant_text = True

    async def gate(p: HookPayload) -> None:
        seen_candidates.append(p.assistant_text)
        if len(seen_candidates) == 1:
            p.reject_final_answer = "[Harness] Run the tests first."

    mgr.register(HookEvent.BEFORE_PROVIDER_REQUEST, hold)
    mgr.register(HookEvent.PRE_FINAL_ANSWER, gate)
    hist = FakeHistory()
    prov = CapturingProvider(_answer_script("All done!", "Ran pytest: 5 passed."))
    events = await _run_collect(prov, mgr, hist=hist)

    assert prov._idx == 2
    assert seen_candidates == ["All done!", "Ran pytest: 5 passed."]
    assert _deltas(events) == "Ran pytest: 5 passed."  # the refused answer was never streamed
    (gate_event,) = _gate_events(events)
    assert gate_event.detail == "[Harness] Run the tests first."
    assert "[Harness] Run the tests first." in prov.system_texts[1]
    assert "## Runtime notes" in prov.system_texts[1]
    assert "Run the tests first" not in prov.system_texts[0]
    assistants = [m for m in hist.rows if m.role == "assistant"]
    assert [b.text for m in assistants for b in m.content if isinstance(b, Text)] == [
        "Ran pytest: 5 passed."
    ]
    assert events[-1].kind == "TurnComplete"


@pytest.mark.asyncio
async def test_without_hold_the_refused_answer_has_already_streamed() -> None:
    mgr = HookManager()

    async def gate(p: HookPayload) -> None:
        if p.assistant_text == "All done!":
            p.reject_final_answer = "[Harness] nope"

    mgr.register(HookEvent.PRE_FINAL_ANSWER, gate)
    events = await _run_collect(CapturingProvider(_answer_script("All done!", "fixed")), mgr)
    assert _deltas(events) == "All done!fixed"  # hold is the hook's responsibility
    assert len(_gate_events(events)) == 1


@pytest.mark.asyncio
async def test_pre_final_answer_rejections_are_capped_per_message() -> None:
    from monkeybot.core.runtime.turn_loop import _FINAL_ANSWER_REJECTIONS

    mgr = HookManager()
    calls = 0

    async def always_reject(p: HookPayload) -> None:
        nonlocal calls
        calls += 1
        p.reject_final_answer = "[Harness] again"

    async def hold(p: HookPayload) -> None:
        p.hold_assistant_text = True

    mgr.register(HookEvent.BEFORE_PROVIDER_REQUEST, hold)
    mgr.register(HookEvent.PRE_FINAL_ANSWER, always_reject)
    answers = [f"answer {i}" for i in range(_FINAL_ANSWER_REJECTIONS + 3)]
    prov = CapturingProvider(_answer_script(*answers))
    events = await _run_collect(prov, mgr, max_turns=20)

    assert len(_gate_events(events)) == _FINAL_ANSWER_REJECTIONS
    assert calls == _FINAL_ANSWER_REJECTIONS  # the hook is not even asked past the cap
    assert _deltas(events) == f"answer {_FINAL_ANSWER_REJECTIONS}"  # first answer past the cap
    assert prov._idx == _FINAL_ANSWER_REJECTIONS + 1
    assert events[-1].kind == "TurnComplete"


@pytest.mark.asyncio
async def test_pre_final_answer_is_not_asked_when_the_turn_budget_is_spent() -> None:
    mgr = HookManager()
    fired = 0

    async def reject(p: HookPayload) -> None:
        nonlocal fired
        fired += 1
        p.reject_final_answer = "[Harness] again"

    mgr.register(HookEvent.PRE_FINAL_ANSWER, reject)
    events = await _run_collect(CapturingProvider(_answer_script("only answer")), mgr, max_turns=1)
    assert fired == 0 and _deltas(events) == "only answer" and not _gate_events(events)


@pytest.mark.asyncio
async def test_pre_final_answer_hook_failure_is_a_pass_through() -> None:
    mgr = HookManager()

    async def boom(p: HookPayload) -> None:
        raise RuntimeError("gate bug")

    async def slow(p: HookPayload) -> None:
        await asyncio.sleep(30)

    mgr.register(HookEvent.PRE_FINAL_ANSWER, boom)
    mgr.register(HookEvent.PRE_FINAL_ANSWER, slow)
    import monkeybot.core.runtime.turn_loop as turn_loop

    original = turn_loop._HOOK_READ_TIMEOUT_S
    turn_loop._HOOK_READ_TIMEOUT_S = 0.05
    try:
        events = await _run_collect(CapturingProvider(_answer_script("fine")), mgr)
    finally:
        turn_loop._HOOK_READ_TIMEOUT_S = original
    assert _deltas(events) == "fine" and not _gate_events(events)
    assert events[-1].kind == "TurnComplete"


@pytest.mark.asyncio
async def test_pre_final_answer_is_skipped_for_tool_turns_and_empty_text() -> None:
    mgr = HookManager()
    candidates: list[str | None] = []

    async def record(p: HookPayload) -> None:
        candidates.append(p.assistant_text)

    mgr.register(HookEvent.PRE_FINAL_ANSWER, record)
    script: list[list[ProviderEvent]] = [
        [TextDelta(text="looking"), ToolCall(call_id="c1", name="run_command", args={}), Done()],
        [TextDelta(text="final"), Done()],
    ]
    await _run_collect(CapturingProvider(script), mgr)
    assert candidates == ["final"]  # tool-calling turns never reach the gate
