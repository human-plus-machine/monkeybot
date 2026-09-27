"""Keep per-chat browser state from leaking between tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _discard_chat_contexts() -> Iterator[None]:
    from browser_mcp import chat_context

    chat_context.discard_isolated()
    yield
    chat_context.discard_isolated()
