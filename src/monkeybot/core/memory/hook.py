"""Memory subsystem hook: captures observations and injects retrieved context.

The :class:`MemoryHook` is the canonical subscriber for :mod:`monkeybot.core.hooks`
events. It implements both halves of the memory lifecycle:

**Write path** (cheap, fire-and-forget, no LLM in the hot path)

* ``USER_MESSAGE`` → append the user's prompt to ``memory/chat_log.md``.
* ``POST_TOOL`` → append the tool name, args, and a truncated result/error to
  ``memory/raw/<ts>_post_tool.md``.
* ``POST_TURN`` → schedule the organizer (debounced; at most one pending run at
  a time, run in the background so the user does not wait).

**Read path** (substring-only retrieval; no LLM, ~ms latency)

* ``PRE_TURN`` → for the user's message, search the memory tree and add hits
  to ``payload.inject_memory_lines`` so they appear in the system prompt.
* ``PRE_TOOL`` → for tools that have a path/query argument
  (``read_file`` / ``write_file`` / ``search_memory`` / ``run_command``), look
  up file-specific or query-specific memories and put them on
  ``payload.inject_text`` so they appear in the next provider call's system
  prompt (the tool result itself is never modified — it stays ground truth).

All write methods serialise on a single :class:`asyncio.Lock` shared with the
organizer to avoid races on ``INDEX.md`` and ``memory/raw/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.memory.storage_ops import async_search_memory_files
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)

_RESULT_PREVIEW_CHARS = 4000
_RAW_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

_DEDUP_TTL_SEC = 300.0
"""5-minute window over which identical (tool, args) calls are deduped."""

_POST_TOOL_SKIP_ON_SUCCESS = frozenset(
    {"read_file", "search_memory", "list_skills"}
)
"""Read-only tools whose successful calls are not captured (errors still are)."""

_CHAT_LOG_FILENAME = "chat_log.md"
"""User messages append here as one line each; the organizer never reads it."""

_CHAT_LOG_HEADER = "# Chat Log\n\nAppend-only log of user messages, newest at bottom.\n"
"""Written once on first append so search has context."""

OrganizerRunner = Callable[[], Awaitable[Any]]
"""Async callable that runs the memory organizer once. Result is ignored."""


def _truncate(value: str | None, limit: int) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[...truncated]"


def _safe_segment(value: str, max_len: int = 60) -> str:
    cleaned = _RAW_FILENAME_RE.sub("-", value).strip("-")
    return cleaned[:max_len] or "x"


def _format_dict(data: dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return repr(data)


def _extract_keywords(text: str, *, max_keywords: int = 5) -> list[str]:
    """Pick a handful of longish word tokens for substring retrieval.

    Drops short stopwords. Phase-1 cheap retrieval, no NLP.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_./-]{3,}", text or "")
    seen: dict[str, None] = {}
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS:
            continue
        if low not in seen:
            seen[low] = None
            if len(seen) >= max_keywords:
                break
    return list(seen.keys())


_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "have",
        "from",
        "your",
        "about",
        "their",
        "there",
        "would",
        "could",
        "should",
        "where",
        "which",
        "while",
        "what",
        "when",
        "then",
        "than",
        "they",
        "them",
        "into",
        "been",
        "were",
        "will",
        "just",
        "also",
        "some",
        "more",
        "very",
        "much",
        "really",
        "thing",
        "things",
        "please",
    }
)


_PRE_TOOL_TOOLS = frozenset(
    {"read_file", "write_file", "search_memory", "run_command"}
)


class _DedupCache:
    """SHA-256 dedup cache with TTL eviction (agentmemory's 5-min pattern)."""

    def __init__(self, ttl_sec: float = _DEDUP_TTL_SEC) -> None:
        self._ttl = float(ttl_sec)
        self._seen: dict[str, float] = {}

    @staticmethod
    def hash(tool_name: str, args: dict[str, Any] | None) -> str:
        """Stable hash over ``(tool_name, sorted-json(args))``."""
        try:
            args_blob = json.dumps(
                args or {}, sort_keys=True, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            args_blob = repr(args)
        payload = f"{tool_name}\0{args_blob}".encode()
        return hashlib.sha256(payload).hexdigest()

    def check_and_record(self, key: str) -> bool:
        """Return ``True`` when ``key`` was seen within TTL (i.e. duplicate)."""
        now = time.monotonic()
        self._evict(now)
        previous = self._seen.get(key)
        self._seen[key] = now
        return previous is not None

    def _evict(self, now: float) -> None:
        if not self._seen:
            return
        expired = [k for k, t in self._seen.items() if (now - t) > self._ttl]
        for k in expired:
            self._seen.pop(k, None)


class MemoryHook:
    """Wire memory writes/reads to :class:`HookManager` events.

    Args:
        storage: Pluggable memory tree backend.
        organizer_runner: Async callable that runs the organizer once. Injected
            so tests can supply a no-op. In production this is
            ``MemoryOrganizer.run``.
        max_retrieval_hits: Cap on hits injected per ``PRE_TURN`` /
            ``PRE_TOOL`` lookup. Default 3.
    """

    def __init__(
        self,
        *,
        storage: WorkspaceStorage,
        organizer_runner: OrganizerRunner | None = None,
        max_retrieval_hits: int = 3,
        dedup_ttl_sec: float = _DEDUP_TTL_SEC,
    ) -> None:
        self._storage = storage
        self._organizer_runner = organizer_runner
        self._max_hits = max(0, int(max_retrieval_hits))
        self._lock = asyncio.Lock()
        self._organizer_pending = False
        self._organizer_task: asyncio.Task[None] | None = None
        self._dedup = _DedupCache(ttl_sec=dedup_ttl_sec)

    def register(self, manager: HookManager) -> None:
        """Attach to every relevant event. Idempotent across distinct managers."""
        manager.register(HookEvent.USER_MESSAGE, self.on_user_message)
        manager.register(HookEvent.PRE_TURN, self.on_pre_turn)
        manager.register(HookEvent.PRE_TOOL, self.on_pre_tool)
        manager.register(HookEvent.POST_TOOL, self.on_post_tool)
        manager.register(HookEvent.POST_TURN, self.on_post_turn)
        manager.register(HookEvent.SESSION_END, self.on_session_end)

    # --------------------------------------------------------------- write

    async def on_user_message(self, payload: HookPayload) -> None:
        """Append the message to ``memory/chat_log.md`` (Lever 3).

        User messages are high-volume and low-signal per message, so they
        bypass the organizer entirely. They land in a flat, append-only log
        that search can still scan when context is needed.
        """
        if not payload.user_message:
            return
        text = payload.user_message.strip()
        if not text:
            return
        await self._append_chat_log(payload, text)

    async def _append_chat_log(self, payload: HookPayload, text: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        flat = " ".join(text.split())
        line = f"- [{ts}] [{payload.thread_id}] {flat}\n"
        async with self._lock:
            try:
                if not await self._storage.exists(_CHAT_LOG_FILENAME):
                    await self._storage.write_text(_CHAT_LOG_FILENAME, _CHAT_LOG_HEADER)
                await self._storage.append_text(_CHAT_LOG_FILENAME, line)
            except OSError as exc:
                logger.warning("memory: failed appending chat_log: %r", exc)

    async def on_post_tool(self, payload: HookPayload) -> None:
        if not payload.tool_name:
            return
        if payload.tool_error is None and payload.tool_name in _POST_TOOL_SKIP_ON_SUCCESS:
            return
        dedup_key = self._dedup.hash(
            payload.tool_name,
            {
                "args": payload.tool_args or {},
                "error": payload.tool_error or "",
            },
        )
        if self._dedup.check_and_record(dedup_key):
            logger.debug(
                "memory: dedup skip for %s (key=%s...)", payload.tool_name, dedup_key[:8]
            )
            return
        result_preview = _truncate(payload.tool_result, _RESULT_PREVIEW_CHARS)
        error_preview = _truncate(payload.tool_error, _RESULT_PREVIEW_CHARS)
        args_str = _format_dict(payload.tool_args or {})
        body = (
            f"# post_tool\n\n"
            f"thread_id: {payload.thread_id}\n"
            f"request_id: {payload.request_id}\n"
            f"timestamp: {int(time.time() * 1000)}\n"
            f"tool: {payload.tool_name}\n\n"
            f"## args\n\n```json\n{args_str}\n```\n\n"
            f"## result\n\n{result_preview or '(empty)'}\n\n"
            f"## error\n\n{error_preview or '(none)'}\n"
        )
        await self._write_raw(f"post_tool_{_safe_segment(payload.tool_name)}", payload.thread_id, body)

    async def on_post_turn(self, payload: HookPayload) -> None:
        del payload
        self._schedule_organizer()

    async def on_session_end(self, payload: HookPayload) -> None:
        del payload
        runner = self._organizer_runner
        if runner is None:
            return
        try:
            async with self._lock:
                await runner()
        except Exception as exc:
            logger.warning("session-end organizer run failed: %r", exc)

    # ---------------------------------------------------------------- read

    async def on_pre_turn(self, payload: HookPayload) -> None:
        if self._max_hits <= 0 or not payload.user_message:
            return
        keywords = _extract_keywords(payload.user_message)
        if not keywords:
            return
        seen: set[str] = set()
        lines: list[str] = []
        for kw in keywords:
            hits = await self._search(kw)
            for hit in hits:
                snippet = hit.get("snippet", "").strip()
                if not snippet or snippet in seen:
                    continue
                seen.add(snippet)
                lines.append(f"- {snippet}")
                if len(lines) >= self._max_hits:
                    break
            if len(lines) >= self._max_hits:
                break
        if lines:
            payload.inject_memory_lines = list(payload.inject_memory_lines) + lines

    async def on_pre_tool(self, payload: HookPayload) -> None:
        if self._max_hits <= 0 or payload.tool_name not in _PRE_TOOL_TOOLS:
            return
        args = payload.tool_args or {}
        candidates: list[str] = []
        for key in ("path", "file_path", "file", "query", "q", "command", "shell", "script"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
        if not candidates:
            return
        seen: set[str] = set()
        snippets: list[str] = []
        for cand in candidates:
            hits = await self._search(cand)
            for hit in hits:
                snippet = hit.get("snippet", "").strip()
                if not snippet or snippet in seen:
                    continue
                seen.add(snippet)
                snippets.append(f"- {snippet}")
                if len(snippets) >= self._max_hits:
                    break
            if len(snippets) >= self._max_hits:
                break
        if snippets:
            heading = (
                f"Relevant memory for {payload.tool_name} "
                f"on {candidates[0]!r}:"
            )
            payload.inject_text = heading + "\n" + "\n".join(snippets)

    # --------------------------------------------------------------- inner

    async def _search(self, query: str) -> list[dict[str, Any]]:
        try:
            result = await async_search_memory_files(
                self._storage,
                query,
                max_hits=self._max_hits * 2,
                skip_relative_prefixes=("raw",),
            )
        except Exception as exc:
            logger.warning("memory search failed for %r: %r", query, exc)
            return []
        hits = result.get("hits") or []
        return hits if isinstance(hits, list) else []

    async def _write_raw(self, kind: str, thread_id: str, body: str) -> None:
        async with self._lock:
            try:
                fname = (
                    f"raw/{int(time.time() * 1000)}_{_safe_segment(thread_id, 20)}_{kind}_"
                    f"{uuid.uuid4().hex[:8]}.md"
                )
                await self._storage.write_text(fname, body)
            except Exception as exc:
                logger.warning("memory raw write failed (%s): %r", kind, exc)

    def _schedule_organizer(self) -> None:
        runner = self._organizer_runner
        if runner is None:
            return
        if self._organizer_pending:
            return
        self._organizer_pending = True

        async def _run() -> None:
            try:
                async with self._lock:
                    await runner()
            except Exception as exc:
                logger.warning("background organizer run failed: %r", exc)
            finally:
                self._organizer_pending = False

        self._organizer_task = asyncio.create_task(_run())


__all__ = ["MemoryHook", "OrganizerRunner"]
