"""Firestore-backed :class:`JobStorage` shipped as a builtin backend.

See 1b-contracts.md §3.3. Each job lives under
``{collection}/{job_id}`` with fields ``job_id``, ``payload``, ``status``,
``leased_until``, ``lease_token`` and ``updated_at``.

``claim_job`` uses a Firestore transaction to guarantee JOB-C-01
(single-winner-under-contention) — the conditional update on
``leased_until`` makes the claim atomic at the document level.

The Firestore SDK is imported lazily so importing this module does not
require ``google-cloud-firestore``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ..base import JobStorage
from ..errors import BackendConfigError

_LEASE_UNTIL_KEY = "leased_until"
_LEASE_TOKEN_KEY = "lease_token"


class FirestoreJobStorage(JobStorage):
    """Firestore-backed ABC-conformant job storage.

    Args:
        project_id: GCP project id. ``None`` uses ADC defaults.
        collection: Name of the Firestore collection holding every job
            document (default ``"scheduler_jobs"``).
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        collection: str = "scheduler_jobs",
    ) -> None:
        self.project_id = project_id
        self.collection = collection
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import firestore  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise BackendConfigError(
                    "FirestoreJobStorage requires emonk[firestore]"
                ) from exc
            self._client = (
                firestore.Client(project=self.project_id)
                if self.project_id
                else firestore.Client()
            )
        return self._client

    def _collection(self) -> Any:
        return self._get_client().collection(self.collection)

    @staticmethod
    def _job_id(job: Mapping[str, Any]) -> str:
        if "job_id" in job:
            return str(job["job_id"])
        if "id" in job:
            return str(job["id"])
        raise BackendConfigError(
            "FirestoreJobStorage: every job must contain a 'job_id' (or 'id') field"
        )

    async def load_jobs(self) -> list[Mapping[str, Any]]:
        """Return every job document under ``collection``."""

        def _stream() -> list[dict[str, Any]]:
            col = self._collection()
            docs: list[dict[str, Any]] = []
            for doc in col.stream():
                data = doc.to_dict() or {}
                data.setdefault("job_id", doc.id)
                docs.append(data)
            return docs

        return await asyncio.to_thread(_stream)

    async def save_jobs(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        """Replace the Firestore collection with ``jobs`` (delete + batch set)."""
        new_jobs = [dict(job) for job in jobs]
        for job in new_jobs:
            self._job_id(job)

        def _write() -> None:
            client = self._get_client()
            col = self._collection()
            batch = client.batch()
            for doc in col.stream():
                batch.delete(doc.reference)
            for job in new_jobs:
                jid = self._job_id(job)
                doc_ref = col.document(jid)
                batch.set(doc_ref, dict(job))
            batch.commit()

        await asyncio.to_thread(_write)

    async def claim_job(
        self, job_id: str, lease_duration_seconds: int = 300
    ) -> bool:
        """Atomically claim ``job_id`` via a Firestore transaction."""

        def _claim() -> bool:
            try:
                from google.cloud import firestore  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise BackendConfigError(
                    "FirestoreJobStorage requires emonk[firestore]"
                ) from exc
            client = self._get_client()
            col = self._collection()
            doc_ref = col.document(job_id)
            transaction = client.transaction()

            @firestore.transactional  # type: ignore[misc]
            def _claim_in_tx(tx: Any) -> bool:
                snap = doc_ref.get(transaction=tx)
                now = datetime.now(UTC)
                lease_until = now + timedelta(seconds=lease_duration_seconds)
                if not getattr(snap, "exists", False):
                    tx.set(
                        doc_ref,
                        {
                            "job_id": job_id,
                            _LEASE_UNTIL_KEY: lease_until,
                            _LEASE_TOKEN_KEY: str(uuid.uuid4()),
                            "updated_at": now,
                        },
                    )
                    return True
                data = snap.to_dict() or {}
                existing_until = data.get(_LEASE_UNTIL_KEY)
                if isinstance(existing_until, str):
                    parsed = datetime.fromisoformat(existing_until)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    existing_until = parsed
                if isinstance(existing_until, datetime):
                    if existing_until.tzinfo is None:
                        existing_until = existing_until.replace(tzinfo=UTC)
                    if existing_until > now:
                        return False
                tx.update(
                    doc_ref,
                    {
                        _LEASE_UNTIL_KEY: lease_until,
                        _LEASE_TOKEN_KEY: str(uuid.uuid4()),
                        "updated_at": now,
                    },
                )
                return True

            return bool(_claim_in_tx(transaction))

        return await asyncio.to_thread(_claim)

    async def release_job(self, job_id: str) -> None:
        """Clear the lease fields on ``job_id`` (no-op if missing)."""

        def _release() -> None:
            doc_ref = self._collection().document(job_id)
            snap = doc_ref.get()
            if not getattr(snap, "exists", False):
                return
            doc_ref.update(
                {
                    _LEASE_UNTIL_KEY: None,
                    _LEASE_TOKEN_KEY: None,
                    "updated_at": datetime.now(UTC),
                }
            )

        await asyncio.to_thread(_release)

    async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        """Return the stored document for ``job_id`` or ``None``."""

        def _read() -> dict[str, Any] | None:
            snap = self._collection().document(job_id).get()
            if not getattr(snap, "exists", False):
                return None
            data = snap.to_dict() or {}
            data.setdefault("job_id", job_id)
            return data

        return await asyncio.to_thread(_read)


__all__ = ["FirestoreJobStorage"]
