"""Per-invocation campaign / memory context for tools that are not path-rewritten by middleware."""

from __future__ import annotations

from contextvars import ContextVar

# Set by MemoryPathMiddleware from config["configurable"]["memory_context_dir"] (or campaign_dir).
memory_context_dir_ctx: ContextVar[str | None] = ContextVar("memory_context_dir_ctx", default=None)
