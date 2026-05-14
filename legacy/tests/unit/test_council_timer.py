from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import monkeybot.cli as cli_module
from monkeybot.cli import _flush_council_on_shutdown, _on_turn_complete


@pytest.fixture(autouse=True)
def reset_timer_state() -> Any:
    """Reset module-level timer state before/after each test."""
    cli_module._council_timers.clear()
    cli_module._background_tasks.clear()
    yield
    for task in list(cli_module._council_timers.values()):
        task.cancel()
    cli_module._council_timers.clear()
    cli_module._background_tasks.clear()


class FakeHistory:
    """Minimal history stub returning a fixed message list."""

    def __init__(self, messages: list[Any] | None = None) -> None:
        self._messages = messages or []

    async def load(self, session_id: str) -> list[Any]:
        return self._messages


class FakeProvider:
    """Placeholder provider; council is monkeypatched, not called directly."""

    pass


async def test_timer_fires_after_idle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Council fires once after the idle timeout expires."""
    council_calls: list[Any] = []

    async def mock_council(*args: Any, **kwargs: Any) -> list[str]:
        council_calls.append(args)
        return []

    monkeypatch.setattr("monkeybot.cli.run_council", mock_council)

    await _on_turn_complete(
        "session1",
        None,
        config={"council": {"enabled": True, "idle_seconds": 0.05}},
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    await asyncio.sleep(0.15)
    assert len(council_calls) == 1


async def test_timer_resets_on_new_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three rapid turns each cancel the previous timer; only one council call fires."""
    council_calls: list[Any] = []

    async def mock_council(*args: Any, **kwargs: Any) -> list[str]:
        council_calls.append(args)
        return []

    monkeypatch.setattr("monkeybot.cli.run_council", mock_council)

    cfg = {"council": {"enabled": True, "idle_seconds": 0.1}}

    await _on_turn_complete(
        "session1",
        None,
        config=cfg,
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    await asyncio.sleep(0.01)
    await _on_turn_complete(
        "session1",
        None,
        config=cfg,
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    await asyncio.sleep(0.01)
    await _on_turn_complete(
        "session1",
        None,
        config=cfg,
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    # 0.05s past third turn start — timer hasn't fired yet
    await asyncio.sleep(0.05)
    assert len(council_calls) == 0
    # 0.15s more — timer has now fired exactly once
    await asyncio.sleep(0.15)
    assert len(council_calls) == 1


async def test_flush_on_shutdown_runs_council(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flush cancels pending timers and runs council immediately for each session."""
    council_calls: list[Any] = []

    async def mock_council(*args: Any, **kwargs: Any) -> list[str]:
        council_calls.append(args)
        return []

    monkeypatch.setattr("monkeybot.cli.run_council", mock_council)

    cfg = {"council": {"enabled": True, "idle_seconds": 300}}

    await _on_turn_complete(
        "session1",
        None,
        config=cfg,
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    await _on_turn_complete(
        "session2",
        None,
        config=cfg,
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )

    await _flush_council_on_shutdown(
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
        council_model="gemini-2.0-flash",
    )

    assert len(council_calls) == 2
    assert len(cli_module._council_timers) == 0


async def test_flush_on_shutdown_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flush with no pending timers completes without error; council not called."""
    council_calls: list[Any] = []

    async def mock_council(*args: Any, **kwargs: Any) -> list[str]:
        council_calls.append(args)
        return []

    monkeypatch.setattr("monkeybot.cli.run_council", mock_council)

    await _flush_council_on_shutdown(
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path="/tmp/noop",
        council_model="gemini-2.0-flash",
    )

    assert len(council_calls) == 0


async def test_disabled_council_no_timer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """council.enabled=False → no timer created."""
    monkeypatch.setattr("monkeybot.cli.run_council", lambda *a, **kw: None)

    await _on_turn_complete(
        "session1",
        None,
        config={"council": {"enabled": False, "idle_seconds": 0.05}},
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    assert len(cli_module._council_timers) == 0


async def test_council_no_config_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing 'council' key in config → no timer created."""
    monkeypatch.setattr("monkeybot.cli.run_council", lambda *a, **kw: None)

    await _on_turn_complete(
        "session1",
        None,
        config={},
        history=FakeHistory(),  # type: ignore[arg-type]
        provider=FakeProvider(),
        memory_path=str(tmp_path),
    )
    assert len(cli_module._council_timers) == 0
