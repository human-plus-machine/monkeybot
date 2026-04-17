"""JSON-file :class:`JobStorage` shipped as a builtin backend.

See 1b-contracts.md §3.3 and §11 (JOB-C-01 … JOB-C-04). The job list is
stored as a flat array under a single JSON file; ``claim_job`` serialises
the read-modify-write cycle with two layers of locking:

* An :class:`asyncio.Lock` serialises concurrent coroutines in the same
  process (``filelock``'s POSIX ``fcntl.flock`` implementation is
  per-process, not per-thread).
* ``filelock.FileLock`` serialises cross-process races (the intended
  production use case — e.g. multiple Cloud Run instances sharing a
  mounted filesystem).

``filelock`` is imported lazily; absent installation raises
:class:`BackendConfigError` so consumers know to install
``emonk[job-storage-json]``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..base import JobStorage
from ..errors import BackendConfigError

_LEASE_UNTIL_KEY = "leased_until"
_LEASE_TOKEN_KEY = "lease_token"


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into an aware :class:`datetime` or ``None``."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class JSONFileJobStorage(JobStorage):
    """File-backed :class:`JobStorage` using JSON + ``filelock``.

    Args:
        path: Target file holding the JSON-encoded job list. Parent
            directories are created on first write. Defaults to
            ``"./jobs.json"``.
        lock_timeout_seconds: Seconds to wait for the cross-process file
            lock before raising :class:`BackendConfigError`.
    """

    def __init__(
        self,
        path: str | Path = "./jobs.json",
        *,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self._path = Path(path)
        self._lock_path = str(self._path) + ".lock"
        self._lock_timeout = float(lock_timeout_seconds)
        self._async_lock = asyncio.Lock()

    @staticmethod
    def _get_filelock_cls() -> type[Any]:
        try:
            from filelock import FileLock
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendConfigError(
                "JSONFileJobStorage requires emonk[job-storage-json] "
                "(install `filelock`)"
            ) from exc
        return FileLock

    def _load_sync(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        raw = self._path.read_text() or "[]"
        data: Any = json.loads(raw)
        if not isinstance(data, list):
            raise BackendConfigError(
                f"JSONFileJobStorage: {self._path} must contain a JSON array"
            )
        return [dict(job) for job in data]

    def _save_sync(self, jobs: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(jobs, indent=2, default=str))

    @staticmethod
    def _job_id(job: Mapping[str, Any]) -> str:
        if "job_id" in job:
            return str(job["job_id"])
        if "id" in job:
            return str(job["id"])
        raise BackendConfigError(
            "JSONFileJobStorage: every job must contain a 'job_id' (or 'id') field"
        )

    async def load_jobs(self) -> list[Mapping[str, Any]]:
        """Return every persisted job document as a list of plain dicts."""

        def _read() -> list[Mapping[str, Any]]:
            filelock_cls = self._get_filelock_cls()
            lock = filelock_cls(self._lock_path, timeout=self._lock_timeout)
            with lock:
                return [dict(job) for job in self._load_sync()]

        async with self._async_lock:
            return await asyncio.to_thread(_read)

    async def save_jobs(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        """Replace the persisted job list with ``jobs`` (per ABC §3.3)."""
        new_jobs = [dict(job) for job in jobs]
        for job in new_jobs:
            self._job_id(job)

        def _write() -> None:
            filelock_cls = self._get_filelock_cls()
            lock = filelock_cls(self._lock_path, timeout=self._lock_timeout)
            with lock:
                self._save_sync(new_jobs)

        async with self._async_lock:
            await asyncio.to_thread(_write)

    async def claim_job(self, job_id: str, lease_duration_seconds: int = 300) -> bool:
        """Atomically claim ``job_id`` if no unexpired lease exists."""

        def _claim() -> bool:
            filelock_cls = self._get_filelock_cls()
            lock = filelock_cls(self._lock_path, timeout=self._lock_timeout)
            with lock:
                jobs = self._load_sync()
                index = next(
                    (i for i, job in enumerate(jobs) if self._job_id(job) == job_id),
                    None,
                )
                now = datetime.now(UTC)
                if index is None:
                    new_job: dict[str, Any] = {
                        "job_id": job_id,
                        _LEASE_UNTIL_KEY: (
                            now + timedelta(seconds=lease_duration_seconds)
                        ).isoformat(),
                        _LEASE_TOKEN_KEY: str(uuid.uuid4()),
                    }
                    jobs.append(new_job)
                    self._save_sync(jobs)
                    return True
                job = jobs[index]
                leased_until = _parse_iso(job.get(_LEASE_UNTIL_KEY))
                if leased_until is not None and leased_until > now:
                    return False
                job[_LEASE_UNTIL_KEY] = (
                    now + timedelta(seconds=lease_duration_seconds)
                ).isoformat()
                job[_LEASE_TOKEN_KEY] = str(uuid.uuid4())
                jobs[index] = job
                self._save_sync(jobs)
                return True

        async with self._async_lock:
            return await asyncio.to_thread(_claim)

    async def release_job(self, job_id: str) -> None:
        """Clear the lease so the next caller can re-claim ``job_id``."""

        def _release() -> None:
            filelock_cls = self._get_filelock_cls()
            lock = filelock_cls(self._lock_path, timeout=self._lock_timeout)
            with lock:
                jobs = self._load_sync()
                for job in jobs:
                    if self._job_id(job) == job_id:
                        job.pop(_LEASE_UNTIL_KEY, None)
                        job.pop(_LEASE_TOKEN_KEY, None)
                self._save_sync(jobs)

        async with self._async_lock:
            await asyncio.to_thread(_release)

    async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        """Return the stored document for ``job_id`` or ``None``."""
        jobs = await self.load_jobs()
        for job in jobs:
            if self._job_id(job) == job_id:
                return dict(job)
        return None


__all__ = ["JSONFileJobStorage"]
