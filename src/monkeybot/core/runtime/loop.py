"""Owned agent loop — public facade for turn semantics.

Callers should import from this module:

- ``run`` — stream ``AgentEvent`` for one user message (ends with ``TurnComplete``)
- ``ToolExecutorPort`` — fakeable tool execution boundary
- ``SUMMARY_TRIGGER_RATIO`` — history-compaction trigger ratio

Implementation lives under ``core/runtime/``:

- ``turn_loop`` — per-turn orchestration (prepare, compact, stream, dispatch)
- ``tool_dispatch`` — tool-batch execution (inspectors, serial/parallel exec)
- ``tool_batch`` — batch-level helpers (rejection, chunking, registry notes)
- ``history_compaction`` — history summarization + budgeted tool-response append
- ``doom_loop`` — consecutive-identical-tool-call detection
- ``loop_hooks`` — hook firing/settlement helpers
- ``loop_messages`` — message / system-prompt shaping helpers
- ``loop_usage`` — usage accounting + provider prompt-token helpers
- ``loop_ports`` — fakeable tool-execution boundary (``ToolExecutorPort``)

Only ``run``, ``ToolExecutorPort``, and ``SUMMARY_TRIGGER_RATIO`` are public here.
Import private helpers directly from their owning module (e.g.
``from .turn_loop import _EMPTY_COMPLETION_RECOVERY_NOTE``), not through this facade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Provider
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.tools.inspector import ToolInspector
from monkeybot.core.types.content_blocks import ContentBlock

from .context_budget import SUMMARY_TRIGGER_RATIO
from .events import AgentEvent, Error, TurnComplete
from .input_admission import InputAdmission
from .loop_hooks import _drain_hook_settlement
from .loop_messages import _normalize_user_content
from .loop_ports import ToolExecutorPort
from .loop_usage import _effective_max_turns, _usage_to_totals
from .turn_loop import _run_inner

__all__ = [
    "SUMMARY_TRIGGER_RATIO",
    "ToolExecutorPort",
    "run",
]

logger = logging.getLogger("monkeybot.core.runtime.loop")


async def run(
    user_content: str | list[ContentBlock],
    ctx: TurnContext,
    *,
    provider: Provider,
    history: HistoryStore,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None = None,
    max_turns: int | None = None,
    hook_manager: HookManager | None = None,
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    vertex_google_search: bool = False,
    input_admission: InputAdmission | None = None,
) -> AsyncIterator[AgentEvent]:
    """Stream agent events for one user message; ends with ``TurnComplete`` (never raises).

    Provider chunks are handled in Gemini-style batches: tool calls accumulate until ``Done``,
    then execute in lexicographic ``call_id`` order for deterministic replay.

    Consecutive ``task`` tool calls and consecutive ``parallel_safe`` tools in one batch run
    concurrently (capped); mutating tools run one chunk at a time in order.

    ``input_admission`` (optional) supplies mid-turn steer injections drained at safe
    boundaries (after tool batches / before the next provider call).

    ``vertex_google_search`` opts into Gemini native ``google_search`` grounding for agent
    turns in this run (main agent or subagent). Only passed through to ``GeminiProvider``;
    internal harness calls (history summarization, memory organizer) never set this flag.
    """
    usage = Usage()
    t0 = time.monotonic()
    trace_id_capture: list[str | None] = [None]
    blocks = _normalize_user_content(user_content)
    effective_max = _effective_max_turns(max_turns, ctx.config)
    logger.debug(
        "harness run start %s",
        kv(
            request_id=ctx.request_id,
            thread_id=ctx.thread_id,
            model=ctx.model,
            max_turns=effective_max,
        ),
    )
    try:
        async for evt in _run_inner(
            blocks,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=tool_executor,
            cancelled=cancelled,
            max_turns=max_turns,
            usage=usage,
            hook_manager=hook_manager,
            trace_id_out=trace_id_capture,
            attachment_store=attachment_store,
            attachment_catalog=attachment_catalog,
            transcript_writer=transcript_writer,
            vertex_google_search=vertex_google_search,
            input_admission=input_admission,
        ):
            yield evt
    except asyncio.CancelledError:
        try:
            cur = asyncio.current_task()
            if cur is not None and getattr(cur, "uncancel", None):
                cur.uncancel()
        except Exception:
            logger.warning(
                "uncancel cleanup failed %s",
                kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                exc_info=True,
            )
        yield Error(request_id=ctx.request_id, error="Request cancelled")
    except Exception as exc:
        logger.exception(
            "harness turn failed %s",
            kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
        )
        yield Error(request_id=ctx.request_id, error=str(exc))
    finally:
        await _drain_hook_settlement(hook_manager)
        usage.duration_ms = int((time.monotonic() - t0) * 1000)
        logger.debug(
            "harness run end %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                duration_ms=usage.duration_ms,
            ),
        )
        yield TurnComplete(
            request_id=ctx.request_id,
            usage=_usage_to_totals(usage),
            trace_id=trace_id_capture[0],
        )
