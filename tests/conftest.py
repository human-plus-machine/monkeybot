"""Shared pytest configuration for the monkeybot test suite."""

import os

import pytest

pytest_plugins = ["tests.observability.conftest"]


@pytest.fixture(autouse=True)
def _default_sandbox_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid accidental SANDBOX_ENABLED leakage between tests (sandbox tests opt in)."""
    monkeypatch.setenv("SANDBOX_ENABLED", "false")


_LAYOUT_ENV_KEYS = (
    "MONKEYBOT_AGENT_ROOT",
    "MONKEYBOT_CONFIG",
    "MONKEYBOT_WORKSPACE_ROOT",
    "SKILLS_PATH",
    "AGENT_MD",
    "MCP_CONFIG",
    "COMMAND_ALLOWLIST_CONFIG",
    "PERMISSION_CONFIG",
    "DB_URL",
    "MEMORY_STORAGE_URI",
    "MONKEYBOT_PYTHON",
)


@pytest.fixture(autouse=True)
def _isolate_exported_layout_environment() -> None:
    """Prevent one gateway lifespan from changing a later test's agent layout."""
    before = {key: os.environ.get(key) for key in _LAYOUT_ENV_KEYS}
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests

    reset_runtime_env_state_for_tests()
