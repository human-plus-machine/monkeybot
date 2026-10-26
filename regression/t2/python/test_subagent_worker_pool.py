"""Worker pool claims a pending run and reclaims one stale claim.

The child is the real ``spawn_subagent`` path pointed at
``testkit/subagent_worker.py``. The stale-claim clock is patched onto
``time.time`` because ``run_worker_loop`` does not take ``now_ms``.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from monkeybot.core.persistence import durable_runs, scheduled_loops
from monkeybot.core.persistence.durable_runs import SQLiteRunStore, SubagentEnvelope, SubagentRunRow
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.subagents.worker_pool import run_worker_loop, worker_env_settings
from testkit.clock import FakeClock

_SCRIPT = Path(__file__).resolve().parents[3] / "testkit" / "subagent_worker.py"
_START_MS = 1_700_000_000_000
_STALE_MS = 5_000


def _envelope(task: str) -> SubagentEnvelope:
    return SubagentEnvelope(
        task=task,
        context="",
        memory_storage_uri="local:///mem",
        parent_run_id="parent-1",
    )


async def _wait_status(
    store: SQLiteRunStore, run_id: str, status: str, timeout_s: float = 15.0
) -> SubagentRunRow:
    deadline = asyncio.get_running_loop().time() + timeout_s
    row: SubagentRunRow | None = None
    while asyncio.get_running_loop().time() < deadline:
        row = await store.get_run(run_id)
        if row is not None and row.status == status:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"{run_id} did not reach {status}; last={row!r}")


@pytest.mark.case("T2-SUB-15")
@pytest.mark.asyncio
async def test_worker_pool_claims_pending_run_and_records_completion(tmp_path: Path) -> None:
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        store = backend.runs()
        scratch = tmp_path / "scratch"
        await store.record_pending(
            run_id="pool-run",
            parent_run_id="parent-1",
            script=str(_SCRIPT),
            envelope=_envelope("roundtrip"),
            scratch_dir=scratch,
        )
        loop = asyncio.create_task(
            run_worker_loop(
                backend,
                worker_id="worker-a",
                script=_SCRIPT,
                poll_interval_s=0.05,
                concurrency=1,
                stale_claim_ms=600_000,
            )
        )
        try:
            row = await _wait_status(store, "pool-run", "completed")
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
        assert row.status == "completed"
        progress = scratch / "progress.jsonl"
        assert progress.is_file()
        assert progress.read_text(encoding="utf-8").strip()
        assert row.result_json is not None
        assert "TurnComplete" in row.result_json
    finally:
        await backend.close()


@pytest.mark.case("T2-SUB-16")
@pytest.mark.asyncio
async def test_stale_claim_is_reclaimed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(_START_MS)

    def _fake_time() -> float:
        return clock.now_ms() / 1000.0

    monkeypatch.setattr(durable_runs.time, "time", _fake_time)
    monkeypatch.setattr(scheduled_loops.time, "time", _fake_time)
    monkeypatch.setenv("MONKEYBOT_WORKER_STALE_CLAIM_MS", str(_STALE_MS))
    settings = worker_env_settings()
    assert settings.stale_claim_ms == _STALE_MS

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        store = backend.runs()
        scratch = tmp_path / "scratch"
        await store.record_pending(
            run_id="stale-run",
            parent_run_id="parent-1",
            script=str(_SCRIPT),
            envelope=_envelope("roundtrip"),
            scratch_dir=scratch,
        )
        assert await store.claim("stale-run", "stale-worker") is True
        assert await store.list_stale_claims(_STALE_MS) == []
        clock.advance(6_000)
        stale = await store.list_stale_claims(_STALE_MS)
        assert [row.run_id for row in stale] == ["stale-run"]

        resets = 0
        original = store.reset_stale_claim

        async def _counting(*args: object, **kwargs: object) -> bool:
            nonlocal resets
            won = await original(*args, **kwargs)
            if won:
                resets += 1
            return won

        monkeypatch.setattr(store, "reset_stale_claim", _counting)
        loop = asyncio.create_task(
            run_worker_loop(
                backend,
                worker_id="worker-b",
                script=_SCRIPT,
                poll_interval_s=0.05,
                concurrency=1,
                stale_claim_ms=settings.stale_claim_ms,
            )
        )
        try:
            row = await _wait_status(store, "stale-run", "completed")
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
        assert resets == 1
        assert row.status == "completed"
        assert await store.list_stale_claims(_STALE_MS) == []
    finally:
        await backend.close()
