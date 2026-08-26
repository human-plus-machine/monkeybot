"""Tool-batch helpers: truncated reject, parallel chunking, registry mutation notes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Any

from monkeybot.core.config.snapshot import current_env
from monkeybot.core.context import (
    LOOPS_REGISTRY_MUTATING_TOOLS,
    MCP_REGISTRY_MUTATING_TOOLS,
    PendingResponseBusPort,
)
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.types_tools import ToolDef

# Max concurrent ``task`` (subagent) subprocesses per single model tool batch.
_MAX_CONCURRENT_SUBAGENTS = 10
_MAX_CONCURRENT_PARALLEL_TOOLS = 10


def _should_reject_tool_batch(
    calls: Sequence[ToolCall],
    *,
    truncated: bool,
) -> bool:
    """True when no tool in this provider turn is safe to execute.

    A length/max-tokens stop means every tool call may carry silently
    incomplete args. Also reject when every call already has ``parse_error``
    (incomplete JSON).
    """
    if not calls:
        return False
    if truncated:
        return True
    return all(c.parse_error for c in calls)


def _rejected_tool_batch_error(
    call: ToolCall,
    *,
    truncated: bool,
) -> str:
    if truncated:
        return (
            f'Tool call "{call.name}" was not executed: the response hit the '
            "output token limit, so its arguments may be truncated. Re-issue "
            "the tool call with complete arguments."
        )
    return call.parse_error or (
        f'Tool call "{call.name}" was not executed: incomplete tool arguments '
        "in this batch. Re-issue with complete arguments."
    )


def _note_registry_mutation(
    call: ToolCall,
    tool_result: ToolExecutionResult,
    *,
    mcp_mutated: bool,
    loops_mutated: bool,
) -> tuple[bool, bool]:
    """Accumulate MCP / loops registry mutation flags after a successful tool call."""
    if tool_result.error is not None:
        return mcp_mutated, loops_mutated
    if call.name in MCP_REGISTRY_MUTATING_TOOLS:
        mcp_mutated = True
    if call.name in LOOPS_REGISTRY_MUTATING_TOOLS:
        loops_mutated = True
    return mcp_mutated, loops_mutated


def _parallel_tool_concurrency() -> int:
    raw = os.environ.get("MONKEYBOT_PARALLEL_TOOL_CONCURRENCY")
    if raw is None or raw.strip() == "":
        return _MAX_CONCURRENT_PARALLEL_TOOLS
    try:
        return max(1, int(raw))
    except ValueError:
        return _MAX_CONCURRENT_PARALLEL_TOOLS


def _parallel_safe_names(tools: Sequence[ToolDef]) -> frozenset[str]:
    """Names marked ``parallel_safe`` (read-only / concurrent-safe tools)."""
    return frozenset(t.name for t in tools if t.parallel_safe)


def _chunk_tool_calls(
    ordered: Sequence[ToolCall],
    *,
    parallel_safe: frozenset[str] | None = None,
) -> list[list[ToolCall]]:
    """Split into maximal runs of consecutive parallelizable tools vs serial singles.

    * Consecutive ``task`` calls may run concurrently (bounded by
      :data:`_MAX_CONCURRENT_SUBAGENTS`).
    * Consecutive tools in ``parallel_safe`` (read-only) may run concurrently
      (bounded by :func:`_parallel_tool_concurrency`).
    * ``task`` never mixes with other parallel-safe tools in one chunk.
    * Mutating / unmarked tools stay sequential chunk-by-chunk.
    """
    safe = parallel_safe or frozenset()
    seq = list(ordered)
    chunks: list[list[ToolCall]] = []
    i = 0
    n = len(seq)
    while i < n:
        name = seq[i].name
        if name == "task":
            j = i + 1
            while j < n and seq[j].name == "task":
                j += 1
            chunks.append(seq[i:j])
            i = j
        elif name in safe:
            j = i + 1
            while j < n and seq[j].name in safe and seq[j].name != "task":
                j += 1
            chunks.append(seq[i:j])
            i = j
        else:
            chunks.append([seq[i]])
            i += 1
    return chunks


async def _await_user_response_any(
    bus: object,
    fut: asyncio.Future[Any],
    pending_key: str,
    *,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Wait for POST/resolve on ``fut`` with optional bus timeout bookkeeping.

    Used by the gateway :func:`_await_user_response` and the inspector confirm path.
    """
    t = (
        timeout_sec
        if timeout_sec is not None
        else float(current_env("PENDING_RESPONSE_TIMEOUT_SEC", "300"))
    )
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=t)
    except TimeoutError:
        ap = getattr(bus, "abandon_pending_timeout", None)
        if callable(ap):
            ap(pending_key)
        return {"_timeout": True}


def confirm_wait_stopped_by_user(
    bus: PendingResponseBusPort | None,
    pending_key: str,
    *,
    cancelled: asyncio.Event | None = None,
) -> bool:
    """True when user Stop abandoned the confirm future.

    ``cancelled`` must already be set. Session DELETE and websocket teardown also
    mark keys terminated via ``abandon_pending_cancel_all`` without setting the
    Event; those paths re-raise instead of settling.
    """
    if bus is None or cancelled is None or not cancelled.is_set():
        return False
    return bus.is_pending_or_terminal(pending_key) == "terminated"
