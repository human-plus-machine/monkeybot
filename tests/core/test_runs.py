"""Tests for ``monkeybot.core.runs``."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from monkeybot.core.runs import cleanup_old_runs, create_scratch_dir, make_run_id


def test_make_run_id_length_and_charset() -> None:
    value = make_run_id()
    allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

    assert len(value) == 26
    assert set(value).issubset(allowed)


def test_make_run_id_unique() -> None:
    a = make_run_id()
    b = make_run_id()
    assert a != b


@pytest.mark.asyncio
async def test_create_scratch_dir_creates_expected_subdirs(tmp_path: Path) -> None:
    base = tmp_path / "runs"
    base.mkdir()
    rid = make_run_id()

    root = await create_scratch_dir(rid, base)

    assert root == base / rid
    assert (root / "input").is_dir()
    assert (root / "memory-promotions").is_dir()


@pytest.mark.asyncio
async def test_create_scratch_dir_raises_if_exists(tmp_path: Path) -> None:
    base = tmp_path / "runs"
    base.mkdir()
    rid = make_run_id()
    await create_scratch_dir(rid, base)

    with pytest.raises(FileExistsError):
        await create_scratch_dir(rid, base)


def age_dir_seconds(path: Path, seconds_ago: int) -> None:
    old_ts = time.time() - seconds_ago
    os.utime(path, (old_ts, old_ts))


@pytest.mark.asyncio
async def test_cleanup_old_runs_deletes_old_completed_only(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)

    old_completed = runs / "old-done"
    old_completed.mkdir(parents=True)
    (old_completed / "output.json").write_text("")
    age_dir_seconds(old_completed, 10 * 24 * 60 * 60)

    stale_in_progress = runs / "old-alive"
    stale_in_progress.mkdir(parents=True)
    age_dir_seconds(stale_in_progress, 10 * 24 * 60 * 60)

    recent_completed = runs / "fresh-done"
    recent_completed.mkdir(parents=True)
    (recent_completed / "output.json").write_text("")

    count = await cleanup_old_runs(runs, older_than_days=7)

    assert count == 1
    assert not old_completed.exists()
    assert stale_in_progress.exists()
    assert recent_completed.exists()


@pytest.mark.asyncio
async def test_cleanup_old_runs_counts_multiple(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    cutoff_seconds = int(12 * 24 * 60 * 60)

    one = runs / "one"
    one.mkdir(parents=True)
    (one / "output.json").touch()
    age_dir_seconds(one, cutoff_seconds)

    two = runs / "two"
    two.mkdir(parents=True)
    (two / "output.json").touch()
    age_dir_seconds(two, cutoff_seconds)

    count = await cleanup_old_runs(runs, older_than_days=7)

    assert count == 2
    assert not one.exists()
    assert not two.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_days", (0, -1))
async def test_cleanup_old_runs_invalid_days_raises(invalid_days: int, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()

    with pytest.raises(ValueError):
        await cleanup_old_runs(runs, older_than_days=invalid_days)
