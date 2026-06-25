"""Shared pytest configuration for the monkeybot test suite."""

import pytest

pytest_plugins = ["tests.observability.conftest"]


@pytest.fixture(autouse=True)
def _default_sandbox_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid accidental SANDBOX_ENABLED leakage between tests (sandbox tests opt in)."""
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
