"""Shared typed message fixtures for provider adapter tests (Story 3)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_rejected_sampling() -> Iterator[None]:
    from monkeybot.providers.rejected_sampling import clear_rejected_sampling

    clear_rejected_sampling()
    yield
    clear_rejected_sampling()
