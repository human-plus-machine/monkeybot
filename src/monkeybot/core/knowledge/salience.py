"""Make the workspace index salient so the model uses `search` unprompted.

Two hooks (F21):

* :class:`IndexAnnouncer` — once per thread, inject a short "index ready"
  notice (file/chunk counts) on ``PRE_TURN`` so the model knows a search
  index exists before it starts exploring.
* :class:`SearchUsageNudge` — drift backstop: if a turn accumulates several
  ``grep`` / ``glob`` / ``read_file`` calls without a single ``search``
  call, inject a one-time reminder on the next tool step.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex

logger = logging.getLogger(__name__)

_EXPLORATION_TOOLS = frozenset({"grep", "glob", "read_file"})
_SEARCH_TOOLS = frozenset({"search"})

# Exploration calls without `search` in one turn before the backstop fires.
_NUDGE_THRESHOLD = 3
_ANNOUNCED_CAP = 256
_NUDGE_THREAD_CAP = 256


@dataclass
class _NudgeState:
    request_id: str | None = None
    exploration_calls: int = 0
    searched: bool = False
    nudged: bool = False

_NUDGE_TEXT = (
    "## Workspace index reminder\n"
    "You have issued several `grep` / `glob` / `read_file` calls this turn "
    "without using `search`. For conceptual, cross-file, or paraphrased "
    "questions the indexed `search` tool usually finds the right files in "
    "one call — try it before more manual exploration."
)


def format_index_announcement(
    files: int,
    chunks: int,
    *,
    embeddings: bool,
    embeddings_degraded_reason: str | None = None,
) -> str:
    layers = "FTS + link graph + embeddings" if embeddings else "FTS + link graph"
    text = (
        "## Workspace knowledge index\n"
        f"A local `search` index over this workspace is ready: {files} files / "
        f"{chunks} chunks ({layers}). Use `search` first for questions about "
        "this codebase; use `grep` for exact identifiers and `glob` to locate "
        "files by name."
    )
    if embeddings_degraded_reason:
        text += (
            "\n\nNote: `knowledge.embeddings.enabled` is set but the semantic "
            f"(ANN) layer is off — {embeddings_degraded_reason}. Search is still "
            "running keyword FTS + link graph only."
        )
    return text


def _append_injection(payload: HookPayload, text: str) -> None:
    if payload.inject_text:
        payload.inject_text = f"{payload.inject_text.rstrip()}\n\n{text}"
    else:
        payload.inject_text = text


class IndexAnnouncer:
    """Inject a one-time per-thread notice that the `search` index is ready."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        embeddings_enabled: bool,
        embeddings_degraded_reason: str | None = None,
        announced_cap: int = _ANNOUNCED_CAP,
    ) -> None:
        self._index = index
        self._embeddings = embeddings_enabled
        self._embeddings_degraded_reason = embeddings_degraded_reason
        self._announced_cap = max(1, announced_cap)
        self._announced: OrderedDict[str, None] = OrderedDict()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.PRE_TURN, self.on_pre_turn)

    def _mark_announced(self, thread_id: str) -> None:
        if thread_id in self._announced:
            self._announced.move_to_end(thread_id)
            return
        self._announced[thread_id] = None
        while len(self._announced) > self._announced_cap:
            self._announced.popitem(last=False)

    async def on_pre_turn(self, payload: HookPayload) -> None:
        if payload.thread_id in self._announced:
            return
        self._mark_announced(payload.thread_id)
        try:
            files, chunks = await self._index.counts()
        except Exception as exc:
            logger.debug("index announcer: counts unavailable (%r)", exc)
            return
        if files <= 0:
            return
        _append_injection(
            payload,
            format_index_announcement(
                files,
                chunks,
                embeddings=self._embeddings,
                embeddings_degraded_reason=self._embeddings_degraded_reason,
            ),
        )
        logger.info(
            "index announcer: announced %d files / %d chunks thread=%s",
            files,
            chunks,
            payload.thread_id,
        )


class SearchUsageNudge:
    """One-shot per-turn reminder when exploration drifts past `search`.

    Counters are scoped per ``thread_id`` so concurrent SSE sessions sharing one
    gateway process cannot reset or inject each other's nudge state.
    """

    def __init__(
        self,
        *,
        threshold: int = _NUDGE_THRESHOLD,
        thread_cap: int = _NUDGE_THREAD_CAP,
    ) -> None:
        self._threshold = threshold
        self._thread_cap = max(1, thread_cap)
        self._by_thread: OrderedDict[str, _NudgeState] = OrderedDict()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.POST_TOOL, self.on_post_tool)
        manager.register(HookEvent.PRE_TOOL, self.on_pre_tool)

    def _state(self, thread_id: str) -> _NudgeState:
        existing = self._by_thread.get(thread_id)
        if existing is not None:
            self._by_thread.move_to_end(thread_id)
            return existing
        state = _NudgeState()
        self._by_thread[thread_id] = state
        while len(self._by_thread) > self._thread_cap:
            self._by_thread.popitem(last=False)
        return state

    def _reset_for(self, state: _NudgeState, request_id: str) -> None:
        state.request_id = request_id
        state.exploration_calls = 0
        state.searched = False
        state.nudged = False

    async def on_post_tool(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        if payload.request_id != state.request_id:
            self._reset_for(state, payload.request_id)
        name = payload.tool_name or ""
        if name in _SEARCH_TOOLS:
            state.searched = True
        elif name in _EXPLORATION_TOOLS:
            state.exploration_calls += 1

    async def on_pre_tool(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        if payload.request_id != state.request_id:
            self._reset_for(state, payload.request_id)
            return
        if state.nudged or state.searched:
            return
        if state.exploration_calls < self._threshold:
            return
        state.nudged = True
        _append_injection(payload, _NUDGE_TEXT)
        logger.info(
            "search usage nudge: injected after %d exploration calls request=%s thread=%s",
            state.exploration_calls,
            payload.request_id,
            payload.thread_id,
        )


__all__ = [
    "IndexAnnouncer",
    "SearchUsageNudge",
    "format_index_announcement",
]
