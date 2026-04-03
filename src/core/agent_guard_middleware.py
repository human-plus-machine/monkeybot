"""Agent middleware: tool output truncation and duplicate tool-error compaction.

Works with LangChain ``AgentMiddleware`` + LangGraph ``add_messages`` (``RemoveMessage``).

Environment (optional overrides)::

    EMONK_THREAD_TOOL_LIMIT     — max tool calls per thread; raises error to stop a looping bot
                                  (default 400, 0 = unset)
    EMONK_RUN_TOOL_LIMIT        — max tool calls per single graph run; raises error to stop a looping bot
                                  (default 120, 0 = unset)
    EMONK_TOOL_LIMIT_BEHAVIOR   — exit_behavior for ToolCallLimitMiddleware:
                                  "continue" (model gets error msgs, default),
                                  "error" (raises ToolCallLimitExceededError, hard stop),
                                  "end" (only valid when limiting a named tool, not global)
    EMONK_TOOL_OUTPUT_MAX_CHARS — truncate tool result strings (default 8000)
    EMONK_ERROR_DEDUP_TAIL      — scan last N messages for duplicate errors (default 64)
    EMONK_SEQUENTIAL_TOOL_CALLS — if 1 (default), only the first tool call per model turn runs;
                                  set 0 to allow parallel tool execution again.

Loop recovery
-------------
Set ``EMONK_TOOL_LIMIT_BEHAVIOR=error`` to hard-stop a bot that enters an
unrecoverable loop.  The ``ToolCallLimitExceededError`` propagates up and
terminates the run immediately.  The default ``"continue"`` lets the model
self-correct by receiving error messages when limits are hit.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from collections.abc import Awaitable, Callable

from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def _tool_content_to_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return str(content)
    except Exception:
        return ""


def _normalize_error_fingerprint(name: str | None, content: str) -> tuple[str, str]:
    """Key for deduplicating repeated tool failures."""
    c = _WS_RE.sub(" ", _tool_content_to_str(content).strip().lower())[:800]
    return (name or "", c)


def _is_error_tool_message(msg: ToolMessage) -> bool:
    status = getattr(msg, "status", None)
    if status == "error":
        return True
    text = _tool_content_to_str(msg.content).lower()
    if not text:
        return False
    needles = (
        "error invoking tool",
        "field required",
        "validation error",
        "invalid json",
        "exception",
        "traceback",
    )
    return any(n in text for n in needles)


class ToolOutputTruncationMiddleware(AgentMiddleware[AgentState[Any], None, Any]):
    """Truncate oversized ``ToolMessage`` content to cap checkpoint / context growth."""

    def __init__(self, *, max_chars: int = 8000) -> None:
        super().__init__()
        self.max_chars = max(256, int(max_chars))
        self.tools = []

    def _shrink(self, msg: ToolMessage) -> ToolMessage:
        raw = _tool_content_to_str(msg.content)
        if len(raw) <= self.max_chars:
            return msg
        truncated = raw[: self.max_chars] + f"\n\n… [truncated {len(raw) - self.max_chars} chars]"
        kwargs: dict[str, Any] = {
            "content": truncated,
            "tool_call_id": msg.tool_call_id,
            "name": msg.name,
        }
        mid = getattr(msg, "id", None)
        if mid is not None:
            kwargs["id"] = mid
        st = getattr(msg, "status", None)
        if st is not None:
            kwargs["status"] = st
        return ToolMessage(**kwargs)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        if isinstance(result, ToolMessage):
            return self._shrink(result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if isinstance(result, ToolMessage):
            return self._shrink(result)
        return result


class DuplicateToolErrorCompactionMiddleware(AgentMiddleware[AgentState[Any], None, Any]):
    """Remove older duplicate error :class:`~langchain_core.messages.tool.ToolMessage` rows in the tail.

    Uses ``RemoveMessage`` so LangGraph's ``add_messages`` reducer drops the
    corresponding entries before the next model call.
    """

    def __init__(self, *, tail_message_scan: int = 64) -> None:
        super().__init__()
        self.tail_message_scan = max(8, int(tail_message_scan))
        self.tools = []

    def before_model(self, state: AgentState[Any], runtime: Any) -> dict[str, Any] | None:
        messages: list[Any] = list(state.get("messages") or [])
        if len(messages) < 2:
            return None

        start = max(0, len(messages) - self.tail_message_scan)
        tail_indices: list[int] = []
        for i in range(start, len(messages)):
            m = messages[i]
            if isinstance(m, ToolMessage) and _is_error_tool_message(m):
                tail_indices.append(i)

        if len(tail_indices) < 2:
            return None

        # Include tool_call_id so sequential failures with identical error text
        # (e.g. repeated edit_file validation) are never collapsed — removing an
        # older ToolMessage would orphan a prior assistant tool_use (API error).
        by_key: dict[tuple[str, str, str], list[int]] = {}
        for i in tail_indices:
            m = messages[i]
            assert isinstance(m, ToolMessage)
            tcid = getattr(m, "tool_call_id", None) or ""
            fp_name, fp_body = _normalize_error_fingerprint(m.name, _tool_content_to_str(m.content))
            key = (tcid, fp_name, fp_body)
            by_key.setdefault(key, []).append(i)

        remove_ids: list[str] = []
        for _key, idxs in by_key.items():
            if len(idxs) <= 1:
                continue
            # Keep newest (largest index); drop older duplicates
            keep = max(idxs)
            for i in idxs:
                if i == keep:
                    continue
                msg = messages[i]
                mid = getattr(msg, "id", None)
                if mid:
                    remove_ids.append(mid)

        if not remove_ids:
            return None

        logger.info(
            "duplicate_tool_error_compaction",
            extra={"removed_message_ids": len(remove_ids)},
        )
        return {"messages": [RemoveMessage(id=mid) for mid in remove_ids]}

    async def abefore_model(self, state: AgentState[Any], runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)


def _filter_ai_content_tool_uses_for_kept_ids(content: Any, kept_tool_call_ids: set[str]) -> Any:
    """Drop ``tool_use`` blocks from message content that are not in *kept_tool_call_ids*.

    Vertex/Anthropic formatting builds API ``tool_use`` from both ``AIMessage.content``
    blocks and ``AIMessage.tool_calls``. If we trim ``tool_calls`` to one entry but leave
    sibling ``tool_use`` blocks in ``content``, those orphans are still sent and the API
    rejects the next request (missing ``tool_result`` for each ``tool_use``).
    """
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    kept_first_tool_use_without_id = False
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        if block.get("type") != "tool_use":
            out.append(block)
            continue
        bid = block.get("id")
        if kept_tool_call_ids:
            if bid is not None and str(bid) in kept_tool_call_ids:
                out.append(block)
            continue
        if not kept_first_tool_use_without_id:
            out.append(block)
            kept_first_tool_use_without_id = True
    return out


class SequentialToolCallsMiddleware(AgentMiddleware[AgentState[Any], None, Any]):
    """Limit each model turn to a single tool call so tools never run in parallel.

    LangGraph's tool node otherwise executes every tool_use in one assistant message
    concurrently (e.g. multiple ``task`` delegations), which interleaves subagents
    and filesystem access. Set ``EMONK_SEQUENTIAL_TOOL_CALLS=0`` to disable.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools = []

    def wrap_model_call(self, request: ModelRequest[Any], handler: Callable[..., Any]) -> Any:
        return self._maybe_trim(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        return self._maybe_trim(await handler(request))

    def _maybe_trim(self, response: Any) -> Any:
        raw = (os.getenv("EMONK_SEQUENTIAL_TOOL_CALLS") or "1").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return response
        if isinstance(response, ExtendedModelResponse):
            return ExtendedModelResponse(
                model_response=self._trim_model_response(response.model_response),
                command=response.command,
            )
        if isinstance(response, ModelResponse):
            return self._trim_model_response(response)
        return response

    def _trim_model_response(self, mr: ModelResponse[Any]) -> ModelResponse[Any]:
        new_result: list[Any] = []
        changed = False
        for m in mr.result:
            if isinstance(m, AIMessage):
                tcs = getattr(m, "tool_calls", None) or []
                if isinstance(tcs, list) and len(tcs) > 1:
                    kept = tcs[:1]
                    kept_ids = {
                        str(tc["id"])
                        for tc in kept
                        if isinstance(tc, dict) and tc.get("id") is not None
                    }
                    new_content = _filter_ai_content_tool_uses_for_kept_ids(m.content, kept_ids)
                    new_result.append(
                        m.model_copy(update={"tool_calls": kept, "content": new_content})
                    )
                    changed = True
                    continue
            new_result.append(m)
        if not changed:
            return mr
        logger.info(
            "sequential_tool_calls_trim",
            extra={"event": "sequential_tool_calls_trim", "kept": 1},
        )
        return ModelResponse(result=new_result, structured_response=mr.structured_response)


def build_default_guard_middleware_stack() -> list[Any]:
    """Stack: tool call limits (LangChain), truncation, duplicate error compaction.

    Appends :class:`SequentialToolCallsMiddleware` last (innermost around the model)
    so only one tool call runs per assistant turn unless ``EMONK_SEQUENTIAL_TOOL_CALLS=0``.

    A single :class:`~langchain.agents.middleware.tool_call_limit.ToolCallLimitMiddleware`
    is added when limits are configured.  Its exit behavior is controlled by
    ``EMONK_TOOL_LIMIT_BEHAVIOR`` (default ``"continue"``).  Set it to ``"error"``
    to hard-stop a bot that enters an unrecoverable loop — the raised
    ``ToolCallLimitExceededError`` terminates the run immediately.

    Returns middleware instances to pass as ``extra_middleware`` /
    ``subagent_middleware`` on :func:`emonk.core.deepagent.build_deep_agent`.
    """
    thread_raw = int(os.getenv("EMONK_THREAD_TOOL_LIMIT", "400"))
    run_raw = int(os.getenv("EMONK_RUN_TOOL_LIMIT", "120"))
    thread_limit = None if thread_raw <= 0 else thread_raw
    run_limit = None if run_raw <= 0 else run_raw

    exit_behavior = os.getenv("EMONK_TOOL_LIMIT_BEHAVIOR", "continue").strip().lower()

    max_chars = int(os.getenv("EMONK_TOOL_OUTPUT_MAX_CHARS", "8000"))
    tail = int(os.getenv("EMONK_ERROR_DEDUP_TAIL", "64"))

    out: list[Any] = []

    if thread_limit is not None or run_limit is not None:
        out.append(
            ToolCallLimitMiddleware(
                tool_name=None,
                thread_limit=thread_limit,
                run_limit=run_limit,
                exit_behavior=exit_behavior,
            )
        )

    out.append(ToolOutputTruncationMiddleware(max_chars=max_chars))
    out.append(DuplicateToolErrorCompactionMiddleware(tail_message_scan=tail))
    out.append(SequentialToolCallsMiddleware())
    return out
