"""Hook firing helpers for the agent turn loop."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Message
from monkeybot.core.types.types_tools import ToolDef

_HOOK_READ_TIMEOUT_S = 2.0
_HOOK_PRE_TOOL_TIMEOUT_S = 1.5
_HOOK_SETTLEMENT_TIMEOUT_S = 2.0


def _settlement_timeout_s() -> float:
    raw = os.environ.get("MONKEYBOT_HOOK_SETTLEMENT_TIMEOUT_S")
    if raw is None or raw.strip() == "":
        return _HOOK_SETTLEMENT_TIMEOUT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _HOOK_SETTLEMENT_TIMEOUT_S


async def _drain_hook_settlement(hook_manager: HookManager | None) -> None:
    """Wait for fire-and-forget hooks before the next provider call / TurnComplete."""
    if hook_manager is None:
        return
    await hook_manager.drain_settlement(timeout_s=_settlement_timeout_s())


def _record_tool_hook_span_event(phase: str, tool_name: str) -> None:
    try:
        from monkeybot.observability.instrumentation import add_tool_hook_span_event

        if phase == "pre_tool":
            add_tool_hook_span_event(phase="pre_tool", tool_name=tool_name)
        else:
            add_tool_hook_span_event(phase="post_tool", tool_name=tool_name)
    except ImportError:
        return


async def _fire_hook(
    hook_manager: HookManager | None,
    *,
    event: HookEvent,
    ctx: TurnContext,
    timeout_s: float,
    user_message: str | None = None,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_result: str | None = None,
    tool_error: str | None = None,
    tools: list[ToolDef] | None = None,
    provider_messages: list[Message] | None = None,
    inner_turn: int | None = None,
    assistant_text: str | None = None,
    thinking_text: str | None = None,
    tool_requests: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    provider_error: str | None = None,
) -> HookPayload | None:
    """Construct + fire a payload when ``hook_manager`` is configured; else return ``None``."""
    if hook_manager is None:
        return None
    payload = HookPayload(
        event=event,
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        ctx=ctx,
        user_message=user_message,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        tool_error=tool_error,
        tools=tools,
        provider_messages=provider_messages,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        thinking_text=thinking_text,
        tool_requests=tool_requests,
        usage=usage,
        provider_error=provider_error,
    )
    return await hook_manager.fire(payload, timeout_s=timeout_s)


def _apply_before_provider_hook(
    payload: HookPayload | None,
    provider_messages: list[Message],
    turn_tools: Sequence[ToolDef],
) -> tuple[list[Message], Sequence[ToolDef], bool]:
    """Return (messages, tools, tools_replaced) after ``BEFORE_PROVIDER_REQUEST``."""
    if payload is None:
        return provider_messages, turn_tools, False
    messages = (
        list(payload.provider_messages)
        if payload.provider_messages is not None
        else provider_messages
    )
    if payload.tools is None:
        return messages, turn_tools, False
    return messages, list(payload.tools), True


async def _fire_after_provider_response(
    hook_manager: HookManager | None,
    *,
    ctx: TurnContext,
    inner_turn: int,
    assistant_text: str | None = None,
    thinking_text: str | None = None,
    tool_requests: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    provider_error: str | None = None,
) -> None:
    await _fire_hook(
        hook_manager,
        event=HookEvent.AFTER_PROVIDER_RESPONSE,
        ctx=ctx,
        timeout_s=0,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        thinking_text=thinking_text,
        tool_requests=tool_requests,
        usage=usage,
        provider_error=provider_error,
    )
