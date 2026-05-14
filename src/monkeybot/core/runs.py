"""Run identifiers, scratch directory layout, and completed-run cleanup."""

from __future__ import annotations

import asyncio
import secrets
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_ulid_payload(payload: bytes) -> str:
    """Encode 16 bytes into a Crockford-base32 ULID string (uppercase length 26)."""
    if len(payload) != 16:
        msg = "ULID payload must be 16 bytes (48-bit timestamp + 80-bit randomness)."
        raise ValueError(msg)
    buffer = int.from_bytes(payload, "big")
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD_ALPHABET[buffer & 0x1F])
        buffer >>= 5
    return "".join(reversed(chars))


def make_run_id() -> str:
    """Return a new ULID string (26 Crockford base32 chars)."""
    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    suffix = secrets.token_bytes(10)
    payload = timestamp_ms.to_bytes(6, "big") + suffix
    return _encode_ulid_payload(payload)


async def create_scratch_dir(run_id: str, base: Path) -> Path:
    """Create ``base/{run_id}/`` with required subdirectories."""
    root = base / run_id
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
    input_dir = root / "input"
    promotions = root / "memory-promotions"
    await asyncio.to_thread(input_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(promotions.mkdir, parents=True, exist_ok=True)
    return root


def _mtime_utc(child: Path) -> datetime:
    st = child.stat()
    return datetime.fromtimestamp(st.st_mtime, tz=UTC)


async def cleanup_old_runs(base: Path, older_than_days: int = 7) -> int:
    """Delete eligible run directories older than cutoff. Returns number deleted."""
    if older_than_days < 1:
        msg = "older_than_days must be >= 1"
        raise ValueError(msg)

    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=older_than_days)

    deleted = 0

    def scan_children() -> list[Path]:
        return sorted(p for p in base.iterdir() if p.is_dir())

    for child in await asyncio.to_thread(scan_children):
        marker = child / "output.json"
        if not await asyncio.to_thread(marker.exists):
            continue
        dir_mtime = await asyncio.to_thread(_mtime_utc, child)
        if not (dir_mtime < cutoff):
            continue
        await asyncio.to_thread(shutil.rmtree, child, False)
        deleted += 1

    return deleted
