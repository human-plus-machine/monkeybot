"""UTC time-bucket helpers for usage series (hour / day / week)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import get_args

from monkeybot.core.llm.usage import UsageGranularity

VALID_GRANULARITIES: frozenset[str] = frozenset(get_args(UsageGranularity))

# Maximum ``since`` window for ``bucket=hour`` (168 buckets per model).
HOUR_BUCKET_MAX_LOOKBACK_MS = 7 * 24 * 60 * 60 * 1000


def coerce_granularity(raw: str | None) -> UsageGranularity:
    """Return a valid granularity, defaulting empty/None to ``day``."""
    if raw is None or raw == "":
        return "day"
    if raw in VALID_GRANULARITIES:
        return raw  # type: ignore[return-value]
    raise ValueError("bucket must be hour, day, or week")


def validate_hour_bucket_window(
    since_ms: int | None,
    granularity: UsageGranularity,
    *,
    now_ms: int | None = None,
) -> None:
    """Reject hour-bucket requests without ``since`` or with an oversized window."""
    if granularity != "hour":
        return
    if since_ms is None:
        raise ValueError("`since` is required when bucket=hour")
    anchor = now_ms if now_ms is not None else int(time.time() * 1000)
    if anchor - since_ms > HOUR_BUCKET_MAX_LOOKBACK_MS:
        raise ValueError("hour bucket window must not exceed 7 days")


def utc_bucket_key(created_at_ms: int, granularity: UsageGranularity) -> str:
    """Format a UTC timestamp into a sortable bucket key."""
    dt = datetime.fromtimestamp(created_at_ms / 1000.0, tz=UTC)
    if granularity == "hour":
        return dt.strftime("%Y-%m-%dT%H")
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    monday = dt.date() - timedelta(days=dt.weekday())
    return monday.isoformat()


def sqlite_bucket_sql(granularity: UsageGranularity) -> str:
    """SQLite expression producing the same keys as :func:`utc_bucket_key`."""
    epoch = "created_at / 1000.0"
    if granularity == "hour":
        return f"strftime('%Y-%m-%dT%H', {epoch}, 'unixepoch')"
    if granularity == "day":
        return f"strftime('%Y-%m-%d', {epoch}, 'unixepoch')"
    return (
        f"date({epoch}, 'unixepoch', '-' || "
        f"((CAST(strftime('%w', {epoch}, 'unixepoch') AS INTEGER) + 6) % 7) || ' days')"
    )


def postgres_bucket_sql(granularity: UsageGranularity) -> str:
    """Postgres expression intended to match :func:`utc_bucket_key`.

    Not exercised in CI (no Postgres service); parity verified by inspection and
    the SQLite behavioural tests in ``tests/core/test_usage.py``.
    """
    utc = "(to_timestamp(created_at / 1000.0) AT TIME ZONE 'UTC')"
    if granularity == "hour":
        return f"to_char({utc}, 'YYYY-MM-DD\"T\"HH24')"
    if granularity == "day":
        return f"to_char({utc}, 'YYYY-MM-DD')"
    return f"to_char((date_trunc('week', {utc}))::date, 'YYYY-MM-DD')"
