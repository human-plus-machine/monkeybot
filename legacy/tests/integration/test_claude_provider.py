"""Integration tests for ClaudeProvider.

test_isinstance_check and test_lazy_import run without any live API key.
test_claude_stream_live is skipped unless ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from monkeybot.core.provider import Message, Provider, ProviderDone, TextDelta
from monkeybot.providers.claude import ClaudeProvider


def test_isinstance_check() -> None:
    """No API key needed — structural subtyping only."""
    os.environ["ANTHROPIC_API_KEY"] = "test-key-for-typecheck"
    try:
        provider = ClaudeProvider()
        assert isinstance(provider, Provider)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_lazy_import() -> None:
    """anthropic must not be imported at module level."""
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", "import monkeybot.providers.claude"],
        capture_output=True,
        timeout=10,
        cwd="/Users/johnpiscani/ez-ai/auriga/automation/monkeybot",
        env={**os.environ, "PYTHONPATH": "src"},
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0, result.stderr.decode()
    assert elapsed_ms < 200, f"Import took {elapsed_ms:.0f}ms — anthropic may be at module level"


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
async def test_claude_stream_live() -> None:
    """Live API: at least one TextDelta and ProviderDone as last event."""
    provider = ClaudeProvider()
    events = []
    async for event in await provider.stream(
        messages=[Message(role="user", content="Reply with exactly the word: pong")],
        tools=[],
        model="claude-3-5-haiku-20241022",
        system="You are a minimal test assistant. Follow instructions exactly.",
    ):
        events.append(event)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    done = [e for e in events if isinstance(e, ProviderDone)]
    assert "pong" in text.lower()
    assert len(done) == 1
    assert events[-1] is done[0], "ProviderDone must be last event"
