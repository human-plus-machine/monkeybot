"""Tests for realtime dependency container."""

from __future__ import annotations

import pytest

from monkeybot.gateway.realtime.deps import RealtimeDependencies


def test_freeze_blocks_mutation() -> None:
    deps = RealtimeDependencies()
    deps.storage = None
    deps.freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        deps.storage = None
