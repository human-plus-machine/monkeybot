"""Pure-logic tests for Firestore usage breakdown (no emulator)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("google.cloud.firestore")

from monkeybot.core.persistence.firestore import FirestoreUsageStore  # noqa: E402


def _ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


@pytest.mark.asyncio
async def test_firestore_breakdown_hour_and_week_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FirestoreUsageStore(None, "prefix")  # type: ignore[arg-type]
    rows = [
        {
            "model": "gemini-2.5-flash",
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_usd": 0.02,
            "created_at": _ms(2026, 7, 26, 9),
        },
        {
            "model": "claude-sonnet-4",
            "input_tokens": 20,
            "output_tokens": 5,
            "cost_usd": 0.08,
            "created_at": _ms(2026, 7, 26, 18),
        },
        {
            "model": "gemini-2.5-flash",
            "input_tokens": 5,
            "output_tokens": 2,
            "cost_usd": 0.01,
            "created_at": _ms(2026, 8, 3, 12),
        },
    ]

    async def _fake_fetch(
        thread_id: str | None,
        since_ms: int | None,
    ) -> list[dict[str, object]]:
        del thread_id
        if since_ms is None:
            return rows
        return [row for row in rows if int(row["created_at"]) >= since_ms]

    monkeypatch.setattr(store, "_fetch_usage_rows", _fake_fetch)

    hourly = await store.breakdown(bucket="hour")
    assert [p.bucket for p in hourly.by_bucket_model] == [
        "2026-07-26T09",
        "2026-07-26T18",
        "2026-08-03T12",
    ]

    weekly = await store.breakdown(bucket="week")
    assert [p.bucket for p in weekly.by_bucket_model] == [
        "2026-07-20",
        "2026-07-20",
        "2026-08-03",
    ]
    assert {p.model for p in weekly.by_bucket_model if p.bucket == "2026-07-20"} == {
        "gemini-2.5-flash",
        "claude-sonnet-4",
    }
