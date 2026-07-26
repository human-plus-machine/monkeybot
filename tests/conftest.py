"""Shared pytest configuration for the monkeybot test suite."""

from __future__ import annotations

import gc
import os
import threading
from typing import Any

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


def _stop_leaked_aiosqlite_connections(*, timeout: float = 0.5) -> int:
    """Force-stop aiosqlite worker threads left open by tests.

    aiosqlite ``Connection`` workers are non-daemon threads blocked on
    ``SimpleQueue.get()``. An unclosed connection makes ``Py_FinalizeEx``
    hang forever after the suite finishes, which looks like a frozen run.
    """
    try:
        import aiosqlite
    except ImportError:
        return 0

    stopped = 0
    for obj in gc.get_objects():
        if not isinstance(obj, aiosqlite.Connection):
            continue
        thread: threading.Thread | None = getattr(obj, "_thread", None)
        if thread is None or not thread.is_alive():
            continue
        try:
            # Prefer the library's own stop path (queues _STOP_RUNNING_SENTINEL).
            stop = getattr(obj, "stop", None)
            if callable(stop):
                stop()
            else:
                continue
        except Exception:
            continue
        thread.join(timeout=timeout)
        stopped += 1
    return stopped


@pytest.fixture(autouse=True)
def _reap_leaked_aiosqlite_after_test() -> Any:
    yield
    _stop_leaked_aiosqlite_connections()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _stop_leaked_aiosqlite_connections()
