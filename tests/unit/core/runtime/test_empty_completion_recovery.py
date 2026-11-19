"""Empty-completion budget refill and thinking-cutoff cap escalation."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.config.snapshot import RuntimeConfig, build_runtime_config
from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta, ToolCall
from monkeybot.core.runtime.events import (
    ContextUsage,
    Error,
    HarnessIntervention,
    TurnComplete,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.turn_loop import (
    _EMPTY_COMPLETION_EXHAUSTED_ERROR,
    _EMPTY_COMPLETION_RECOVERY_NOTE,
    _TRUNCATED_THINKING_EXHAUSTED_ERROR,
    _TRUNCATED_THINKING_NOTE,
)
from monkeybot.core.testing.mocks_provider import ScriptedProvider
from monkeybot.core.tools.types import ToolExecutionResult
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import FakeProvider

_GLM = "us.zai.glm-5.3"
_WINDOW = 15_000


def _cap_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_tokens: int) -> RuntimeConfig:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        f"model:\n  max_tokens: {max_tokens}\n",
        encoding="utf-8",
    )
    return build_runtime_config(agent_root=tmp_path)


def _ctx_for(
    *,
    model: str = _GLM,
    config: RuntimeConfig | None = None,
    context_window_tokens: int = 200_000,
) -> TurnContext:
    ctx = _ctx(model=model, context_window_tokens=context_window_tokens)
    if config is None:
        return ctx
    return TurnContext(
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        agent_md=ctx.agent_md,
        memory_index=ctx.memory_index,
        skills=ctx.skills,
        tools=list(ctx.tools),
        user_id=ctx.user_id,
        parent_run_id=ctx.parent_run_id,
        model=ctx.model,
        context_window_tokens=ctx.context_window_tokens,
        config=config,
    )


def _tool(call_id: str = "c1") -> list[object]:
    return [ToolCall(call_id=call_id, name="run_command", args={"command": "echo hi"}), Done()]


def _cutoff() -> list[object]:
    return [ThinkingDelta(text="still reasoning"), Done(truncated=True)]


def _answer() -> list[object]:
    return [TextDelta(text="Here is a real answer."), Done()]


def _caps(provider: ScriptedProvider) -> list[int | None]:
    found: list[int | None] = []
    for hint in provider.stream_hints:
        found.append(None if hint is None else hint.max_tokens)
    return found


def _details(events: list[object], name: str) -> list[str]:
    return [
        event.detail
        for event in events
        if isinstance(event, HarnessIntervention) and event.intervention == name
    ]


def _joined(messages: list[object], *, system_only: bool) -> str:
    parts: list[str] = []
    for message in messages:
        if system_only and message.role != "system":  # type: ignore[attr-defined]
            continue
        for block in message.content:  # type: ignore[attr-defined]
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
    return "".join(parts)


async def _drive(
    provider: object,
    ctx: TurnContext,
    *,
    max_turns: int = 8,
) -> list[object]:
    events: list[object] = []
    async for event in run(
        "please do the thing",
        ctx,
        provider=provider,  # type: ignore[arg-type]
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text("ok")),
        max_turns=max_turns,
    ):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_post_tool_empty_budget_refills_after_a_tool_call() -> None:
    """Tool, empty, tool, empty, text: the second empty still gets a retry."""
    provider = FakeProvider([_tool("c1"), [Done()], _tool("c2"), [Done()], _answer()])
    events = await _drive(provider, _ctx())

    assert provider.stream_calls == 5
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    assert _details(events, "post_tool_empty") == ["retry", "retry"]


@pytest.mark.asyncio
async def test_truncated_text_does_not_refill_the_empty_budget() -> None:
    """A cut-off text reply is not progress, so the next silence still exhausts."""
    provider = FakeProvider(
        [
            [Done()],
            [Done()],
            [TextDelta(text="partial"), Done(truncated=True)],
            [Done()],
        ]
    )
    events = await _drive(provider, _ctx())

    assert provider.stream_calls == 4
    assert _details(events, "empty_completion") == ["retry", "retry", "exhausted"]
    assert _details(events, "truncated_text") == ["continue"]
    assert [event.error for event in events if isinstance(event, Error)] == [
        _EMPTY_COMPLETION_EXHAUSTED_ERROR
    ]


@pytest.mark.asyncio
async def test_thinking_cutoff_retries_with_a_doubled_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_cutoff(), _answer()], name="bedrock")
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 8192))
    events = await _drive(provider, ctx)

    assert _caps(provider) == [None, 16_384]
    assert _details(events, "truncated_thinking") == ["escalate"]
    assert _details(events, "empty_completion") == []
    sent = _joined(provider.stream_messages[1], system_only=False)
    system = _joined(provider.stream_messages[1], system_only=True)
    assert _TRUNCATED_THINKING_NOTE in sent
    assert _TRUNCATED_THINKING_NOTE not in system
    assert _EMPTY_COMPLETION_RECOVERY_NOTE not in sent
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)


@pytest.mark.asyncio
async def test_raised_cap_doubles_and_persists_after_a_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [_cutoff(), _cutoff(), _tool("c1"), _answer()],
        name="bedrock",
    )
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 8192))
    events = await _drive(provider, ctx)

    assert _caps(provider) == [None, 16_384, 32_768, 32_768]
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)


@pytest.mark.asyncio
async def test_raised_cap_never_exceeds_the_model_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_cutoff(), _cutoff()], name="bedrock")
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 100_000))
    events = await _drive(provider, ctx)

    assert _caps(provider) == [None, 128_000]
    assert provider.stream_calls == 2
    assert _details(events, "truncated_thinking") == ["escalate", "exhausted"]
    assert [event.error for event in events if isinstance(event, Error)] == [
        _TRUNCATED_THINKING_EXHAUSTED_ERROR
    ]


@pytest.mark.asyncio
async def test_thinking_cutoff_retries_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_cutoff(), _cutoff(), _cutoff(), _cutoff()], name="bedrock")
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 8192))
    events = await _drive(provider, ctx)

    assert provider.stream_calls == 4
    assert _caps(provider) == [None, 16_384, 32_768, 65_536]
    assert _details(events, "truncated_thinking") == [
        "escalate",
        "escalate",
        "escalate",
        "exhausted",
    ]
    assert _details(events, "empty_completion") == []
    assert [event.error for event in events if isinstance(event, Error)] == [
        _TRUNCATED_THINKING_EXHAUSTED_ERROR
    ]


@pytest.mark.asyncio
async def test_raised_cap_stays_within_the_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_cutoff(), _answer()], name="bedrock")
    ctx = _ctx_for(
        config=_cap_config(tmp_path, monkeypatch, 8192),
        context_window_tokens=_WINDOW,
    )
    events = await _drive(provider, ctx)
    usages = [event for event in events if isinstance(event, ContextUsage)]
    room = _WINDOW - usages[0].estimated_tokens

    assert room > 8192
    assert _caps(provider)[0] is None
    assert _caps(provider)[1] == min(16_384, 128_000, room)
    assert _caps(provider)[1] is not None and _caps(provider)[1] < 16_384


@pytest.mark.asyncio
async def test_thinking_cutoff_after_tools_keeps_the_post_tool_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_tool(), _cutoff(), [Done()], _answer()], name="bedrock")
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 8192))
    events = await _drive(provider, ctx)

    assert provider.stream_calls == 4
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)
    assert _details(events, "truncated_thinking") == ["escalate"]
    assert _details(events, "post_tool_empty") == ["retry"]


@pytest.mark.asyncio
async def test_progress_refills_cutoff_retries_without_resetting_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [_cutoff(), _cutoff(), _cutoff(), _tool(), _cutoff(), _answer()],
        name="bedrock",
    )
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 8192))
    events = await _drive(provider, ctx, max_turns=10)

    assert _caps(provider) == [None, 16_384, 32_768, 65_536, 65_536, 128_000]
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(event, Error) for event in events)


@pytest.mark.asyncio
async def test_cutoff_on_a_provider_without_override_uses_the_empty_path() -> None:
    provider = ScriptedProvider([_cutoff(), _answer()], name="fake")
    events = await _drive(provider, _ctx_for())

    assert _details(events, "empty_completion") == ["retry"]
    assert _details(events, "truncated_thinking") == []
    assert _caps(provider) == [None, None]
    assert not any(isinstance(event, Error) for event in events)


@pytest.mark.asyncio
async def test_cutoff_for_an_unknown_model_uses_the_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_cutoff(), _answer()], name="bedrock")
    ctx = _ctx_for(
        model="us.meta.llama4-maverick",
        config=_cap_config(tmp_path, monkeypatch, 8192),
    )
    events = await _drive(provider, ctx)

    assert _details(events, "empty_completion") == ["retry"]
    assert _details(events, "truncated_thinking") == []
    assert _caps(provider) == [None, None]


@pytest.mark.asyncio
async def test_cutoff_at_the_model_limit_uses_the_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_cutoff(), _answer()], name="bedrock")
    ctx = _ctx_for(config=_cap_config(tmp_path, monkeypatch, 128_000))
    events = await _drive(provider, ctx)

    assert _details(events, "empty_completion") == ["retry"]
    assert _details(events, "truncated_thinking") == []
    assert _caps(provider) == [None, None]
