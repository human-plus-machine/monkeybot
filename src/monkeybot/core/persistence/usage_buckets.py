"""UTC time-bucket helpers for usage series (hour / day / week)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from monkeybot.core.llm.usage import UsageGranularity

VALID_GRANULARITIES: frozenset[str] = frozenset({"hour", "day", "week"})


def coerce_granularity(raw: str | None) -> UsageGranularity:
    """Return a valid granularity, defaulting empty/None to ``day``."""
    if raw is None or raw == "":
        return "day"
    if raw in VALID_GRANULARITIES:
        return raw  # type: ignore[return-value]
    raise ValueError(f"bucket must be hour, day, or week, got {raw!r}")


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
    """Postgres expression producing the same keys as :func:`utc_bucket_key`."""
    utc = "(to_timestamp(created_at / 1000.0) AT TIME ZONE 'UTC')"
    if granularity == "hour":
        return f"to_char({utc}, 'YYYY-MM-DD\"T\"HH24')"
    if granularity == "day":
        return f"to_char({utc}, 'YYYY-MM-DD')"
    return f"to_char((date_trunc('week', {utc}))::date, 'YYYY-MM-DD')"
