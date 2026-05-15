"""
Mock implementations for Gateway testing.

Provides :class:`MockAgentCore` — a simple echo implementation for exercising
the gateway without a live LLM stack.
"""

from __future__ import annotations

from . import interfaces


class MockAgentCore(interfaces.AgentCoreInterface):
    """Minimal agent core for gateway tests (echo + trace_id)."""

    async def process_message(self, user_id: str, content: str, trace_id: str) -> str:
        return f"Echo: {content} (trace: {trace_id})"
