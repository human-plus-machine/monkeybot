"""E3 integration tests — Observability, Scheduling & Cost Tracking.

Exercises the cross-story flows:
  AgentLoop.on_turn_complete → record_usage → get_usage_summary
  monkeybot usage CLI command with real DB data
  Scheduler._tick() fires an overdue job constructed from a config-like dict
"""
from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.events import TurnComplete
from monkeybot.core.history import ConversationHistory
from monkeybot.core.loop import AgentLoop
from monkeybot.core.provider import (
    Message,
    Provider,
    ProviderDone,
    ProviderUsage,
    TextDelta,
    ToolDef,
)
from monkeybot.core.scheduler import JobConfig, Scheduler
from monkeybot.core.usage import get_usage_summary, record_usage

# ---------------------------------------------------------------------------
# FakeProvider (same pattern as test_e2e.py)
# ---------------------------------------------------------------------------


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, event_batches: list[list]) -> None:
        self._batches = iter(event_batches)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str,
        system: str,
        context: object = None,
    ) -> object:
        batch = next(self._batches)

        async def _gen() -> object:
            for event in batch:
                yield event

        return _gen()


assert isinstance(FakeProvider([[]]), Provider)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path: Path) -> tuple[Path, ConversationHistory]:
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# TestBot\nYou are a test bot.")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()
    history = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await history.init()
    return tmp_path, history


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "usage.db")


# ---------------------------------------------------------------------------
# Tests — on_turn_complete → record_usage
# ---------------------------------------------------------------------------


async def test_on_turn_complete_records_usage(env: tuple) -> None:
    """AgentLoop with on_turn_complete wired records a turn_usage row after each turn."""
    tmp_path, history = env
    db = str(tmp_path / "test.db")

    recorded: list[tuple[str, TurnComplete]] = []

    async def _callback(session_id: str, event: TurnComplete) -> None:
        await record_usage(db, session_id, event)
        recorded.append((session_id, event))

    provider = FakeProvider([[
        TextDelta(text="Hello!"),
        ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=5)),
    ]])
    loop = AgentLoop(
        provider=provider,  # type: ignore[arg-type]
        history=history,
        inspectors=[],
        config={
            "agent_md_path": str(tmp_path / "AGENT.md"),
            "memory_path": str(tmp_path / "memory"),
            "skills_path": str(tmp_path / "skills"),
            "bot_dir": str(tmp_path),
            "model": "fake",
        },
        on_turn_complete=_callback,
    )

    events = [e async for e in loop.run("Hi", "sess-e3")]

    turn_completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_completes) == 1

    # Verify usage was recorded
    assert len(recorded) == 1
    session_id, tc = recorded[0]
    assert session_id == "sess-e3"
    assert tc.input_tokens == 10
    assert tc.output_tokens == 5

    # Verify it's queryable via get_usage_summary
    summary = await get_usage_summary(db, since_hours=1.0)
    assert summary.turns == 1
    assert summary.input_tokens == 10
    assert summary.output_tokens == 5


async def test_multiple_turns_accumulate_in_usage(env: tuple) -> None:
    """Two turns produce two rows; get_usage_summary aggregates both."""
    tmp_path, history = env
    db = str(tmp_path / "test.db")

    async def _callback(session_id: str, event: TurnComplete) -> None:
        await record_usage(db, session_id, event)

    for i, tokens in enumerate([(10, 5), (20, 8)]):
        inp, out = tokens
        provider = FakeProvider([[
            TextDelta(text="ok"),
            ProviderDone(usage=ProviderUsage(input_tokens=inp, output_tokens=out)),
        ]])
        loop = AgentLoop(
            provider=provider,  # type: ignore[arg-type]
            history=history,
            inspectors=[],
            config={
                "agent_md_path": str(tmp_path / "AGENT.md"),
                "memory_path": str(tmp_path / "memory"),
                "skills_path": str(tmp_path / "skills"),
                "bot_dir": str(tmp_path),
                "model": "fake",
            },
            on_turn_complete=_callback,
        )
        [_ async for _ in loop.run(f"turn {i}", "sess-multi")]

    summary = await get_usage_summary(db, since_hours=1.0)
    assert summary.turns == 2
    assert summary.input_tokens == 30
    assert summary.output_tokens == 13


# ---------------------------------------------------------------------------
# Tests — usage CLI command via CliRunner
# ---------------------------------------------------------------------------


def test_usage_cli_empty(tmp_path: Path) -> None:
    """monkeybot usage on empty DB prints 'No usage data found.'"""
    from click.testing import CliRunner

    from monkeybot.cli import main

    db = str(tmp_path / "empty.db")
    runner = CliRunner()
    result = runner.invoke(main, ["usage", "--since", "24"], env={"DB_URL": f"sqlite:///{db}"})
    assert result.exit_code == 0
    assert "No usage data found." in result.output


def test_usage_cli_with_data(tmp_path: Path) -> None:
    """monkeybot usage prints formatted summary when turn_usage has rows."""
    import sqlite3
    import time as _time

    from click.testing import CliRunner

    from monkeybot.cli import main

    db = str(tmp_path / "usage.db")
    # Seed with raw sqlite3 — avoids calling asyncio.run() inside a running event loop
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE turn_usage ("
        "id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, "
        "cached_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0, "
        "duration_ms INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO turn_usage VALUES (?,?,?,?,?,?,?,?,?)",
        ("ID1", "R1", "s1", 100, 50, 0, 0.0, 300, int(_time.time() * 1000)),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(main, ["usage", "--since", "24"], env={"DB_URL": f"sqlite:///{db}"})
    assert result.exit_code == 0
    assert "Turns" in result.output
    assert "100" in result.output
    assert "50" in result.output


# ---------------------------------------------------------------------------
# Tests — Scheduler integration with config-like dict
# ---------------------------------------------------------------------------


async def test_scheduler_fires_job_from_config_dict(tmp_path: Path) -> None:
    """Scheduler built from a config dict fires an overdue job on first _tick()."""
    db = str(tmp_path / "sched.db")
    fired: list[str] = []

    async def _job() -> None:
        fired.append("fired")

    job = JobConfig(name="test-job", cron="0 * * * *", callable="os.path:exists", enabled=True)
    scheduler = Scheduler(db_path=db, jobs=[job], poll_interval=60)

    # Pre-load the callable manually (bypassing importlib for test isolation)
    await scheduler._init_db()
    scheduler._callables["test-job"] = _job  # type: ignore[attr-defined]

    await scheduler._tick()  # type: ignore[attr-defined]

    assert len(fired) == 1


async def test_scheduler_start_stop_cleans_up(tmp_path: Path) -> None:
    """Scheduler.start() + stop() leaves no dangling tasks."""
    db = str(tmp_path / "sched.db")
    scheduler = Scheduler(db_path=db, jobs=[], poll_interval=60)

    await scheduler.start()
    assert scheduler._task is not None  # type: ignore[attr-defined]

    await scheduler.stop()
    assert scheduler._task is None  # type: ignore[attr-defined]
