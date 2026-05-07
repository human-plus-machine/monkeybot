"""Mongo-backed :class:`JobStorage` shipped as a builtin backend.

See 1b-contracts.md §3.3. Jobs live as documents in
``{database}.{collection}`` keyed by ``_id == job_id``. ``claim_job``
issues a single ``findOneAndUpdate`` predicated on
``{leased_until: null} | {leased_until: {$lt: now}}`` — the document
atomicity of Mongo makes this a safe JOB-C-01 implementation without
requiring a replica-set transaction.

Motor is imported lazily inside :mod:`_mongo_client`; this module is
safe to import without the optional dependency installed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .._mongo_client import get_client
from ..base import JobStorage
from ..errors import BackendConfigError

_LEASE_UNTIL_KEY = "leased_until"
_LEASE_TOKEN_KEY = "lease_token"


class MongoJobStorage(JobStorage):
    """ABC-conformant :class:`JobStorage` backed by MongoDB.

    Args:
        uri_env: Env var holding the Mongo connection URI (default
            ``"MONGO_URI"``).
        database: Target database (default ``"emonk"``).
        collection: Target collection (default ``"jobs"``).
    """

    def __init__(
        self,
        *,
        uri_env: str = "MONGO_URI",
        database: str = "emonk",
        collection: str = "jobs",
    ) -> None:
        self.uri_env = uri_env
        self.database = database
        self.collection_name = collection
        self._collection: Any = None
        self._indexes_ready = False

    async def _ensure_collection(self) -> Any:
        if self._collection is not None:
            return self._collection
        client = await get_client(uri_env=self.uri_env)
        collection = client[self.database][self.collection_name]
        if not self._indexes_ready:
            await collection.create_index(_LEASE_UNTIL_KEY)
            self._indexes_ready = True
        self._collection = collection
        return collection

    @staticmethod
    def _job_id(job: Mapping[str, Any]) -> str:
        if "job_id" in job:
            return str(job["job_id"])
        if "id" in job:
            return str(job["id"])
        if "_id" in job:
            return str(job["_id"])
        raise BackendConfigError(
            "MongoJobStorage: every job must contain a 'job_id' (or 'id') field"
        )

    def _doc_to_job(self, doc: Mapping[str, Any]) -> dict[str, Any]:
        job = {k: v for k, v in doc.items() if k != "_id"}
        job["job_id"] = doc.get("_id")
        return job

    async def load_jobs(self) -> list[Mapping[str, Any]]:
        """Return every document in ``collection`` as a plain dict."""
        collection = await self._ensure_collection()
        cursor = collection.find({})
        docs = await cursor.to_list(length=None)
        return [self._doc_to_job(doc) for doc in docs]

    async def save_jobs(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        """Replace every document in ``collection`` with ``jobs``."""
        new_jobs = [dict(job) for job in jobs]
        for job in new_jobs:
            self._job_id(job)
        collection = await self._ensure_collection()
        await collection.delete_many({})
        if not new_jobs:
            return
        payloads: list[dict[str, Any]] = []
        for job in new_jobs:
            jid = self._job_id(job)
            doc = {k: v for k, v in job.items() if k not in {"job_id", "id", "_id"}}
            doc["_id"] = jid
            payloads.append(doc)
        await collection.insert_many(payloads)

    async def save_job(self, job: Mapping[str, Any]) -> None:
        """Upsert one job document without changing lease ownership fields."""
        new_job = dict(job)
        jid = self._job_id(new_job)
        doc = {
            k: v
            for k, v in new_job.items()
            if k not in {"job_id", "id", "_id", _LEASE_UNTIL_KEY, _LEASE_TOKEN_KEY}
        }
        collection = await self._ensure_collection()
        await collection.update_one({"_id": jid}, {"$set": doc}, upsert=True)

    async def claim_job(
        self, job_id: str, lease_duration_seconds: int = 300
    ) -> bool:
        """Atomically claim ``job_id`` via ``find_one_and_update``."""
        collection = await self._ensure_collection()
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_duration_seconds)
        existing = await collection.find_one({"_id": job_id})
        if existing is None:
            try:
                await collection.insert_one(
                    {
                        "_id": job_id,
                        _LEASE_UNTIL_KEY: lease_until,
                        _LEASE_TOKEN_KEY: str(uuid.uuid4()),
                        "updated_at": now,
                    }
                )
                return True
            except Exception:  # noqa: BLE001 - duplicate-key race is expected
                pass
        result = await collection.find_one_and_update(
            {
                "_id": job_id,
                "$or": [
                    {_LEASE_UNTIL_KEY: None},
                    {_LEASE_UNTIL_KEY: {"$exists": False}},
                    {_LEASE_UNTIL_KEY: {"$lt": now}},
                ],
            },
            {
                "$set": {
                    _LEASE_UNTIL_KEY: lease_until,
                    _LEASE_TOKEN_KEY: str(uuid.uuid4()),
                    "updated_at": now,
                }
            },
        )
        return result is not None

    async def release_job(self, job_id: str) -> None:
        """Clear the lease fields on ``job_id`` (no-op if missing)."""
        collection = await self._ensure_collection()
        await collection.update_one(
            {"_id": job_id},
            {
                "$set": {
                    _LEASE_UNTIL_KEY: None,
                    _LEASE_TOKEN_KEY: None,
                    "updated_at": datetime.now(UTC),
                }
            },
        )

    async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        """Return the document for ``job_id`` or ``None`` if missing."""
        collection = await self._ensure_collection()
        doc = await collection.find_one({"_id": job_id})
        if doc is None:
            return None
        return self._doc_to_job(doc)


__all__ = ["MongoJobStorage"]
