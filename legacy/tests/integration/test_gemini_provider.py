"""Integration tests for GeminiProvider.

test_isinstance_check and test_lazy_import run without any API key.
test_real_text_response is skipped unless GEMINI_API_KEY is set.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from monkeybot.core.provider import (
    Message,
    Provider,
    ProviderDone,
    TextDelta,
)
from monkeybot.providers.gemini import GeminiProvider


def test_isinstance_check() -> None:
    """No API key needed — pure structural subtyping test."""
    assert isinstance(GeminiProvider(), Provider)


def test_lazy_import() -> None:
    """Module import must not pull in google-genai at module level."""
    import sys

    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", "import monkeybot.providers.gemini"],
        capture_output=True,
        timeout=10,
        cwd="/Users/johnpiscani/ez-ai/auriga/automation/monkeybot",
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0, result.stderr.decode()
    assert elapsed_ms < 200, (
        f"Import took {elapsed_ms:.0f}ms — google-genai may be at module level"
    )


@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="No GEMINI_API_KEY set")
@pytest.mark.asyncio
async def test_real_text_response() -> None:
    """Live API test: verifies streaming yields text and ProviderDone."""
    provider = GeminiProvider()
    events = []
    async for event in await provider.stream(
        messages=[Message(role="user", content="Reply with exactly the word: pong")],
        tools=[],
        model="gemini-2.0-flash",
        system="You are a minimal test assistant. Follow instructions exactly.",
    ):
        events.append(event)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    done = [e for e in events if isinstance(e, ProviderDone)]
    assert "pong" in text.lower()
    assert len(done) == 1
    assert done[0].usage.input_tokens > 0
    assert events[-1] is done[0], "ProviderDone must be last event"
