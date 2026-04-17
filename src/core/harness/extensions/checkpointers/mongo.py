"""Mongo-backed :class:`Checkpointer` shipped as a builtin backend.

See 1b-contracts.md §8.2 for the collection shape. The connection is shared
via :mod:`src.core.harness.extensions._mongo_client`. Document-level
atomicity is relied upon; no multi-document transactions are issued (replica
set optional per 1B).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from .._mongo_client import get_client
from ..base import Checkpointer
from ..errors import BackendConfigError, CheckpointerError, CheckpointMissing
from ..values import CheckpointRef


def _new_ulid() -> str:
    """Monotonic-ish id: nanosecond timestamp hex + UUID suffix."""
    return f"{time.time_ns():016x}-{uuid.uuid4().hex[:8]}"


class MongoCheckpointer(Checkpointer):
    """Mongo-backed ABC-conformant checkpointer.

    State is stored as a nested BSON document under ``state`` — Mongo handles
    native types. ``checkpoint_id`` is ULID-flavored and indexed alongside
    ``session_id`` for newest-first reads.
    """

    def __init__(
        self,
        *,
        uri_env: str = "MONGO_URI",
        database: str = "emonk",
        collection: str = "checkpoints",
        require_replica_set: bool = False,
    ) -> None:
        self.uri_env = uri_env
        self.database = database
        self.collection_name = collection
        self.require_replica_set = require_replica_set
        self._collection: Any = None
        self._indexes_ready = False

    async def _ensure_collection(self) -> Any:
        if self._collection is not None:
            return self._collection
        client = await get_client(uri_env=self.uri_env)
        if self.require_replica_set:
            hello = await client.admin.command("hello")
            if not hello.get("setName"):
                raise BackendConfigError(
                    "MongoCheckpointer requires a replica set but the cluster is standalone"
                )
        collection = client[self.database][self.collection_name]
        if not self._indexes_ready:
            await collection.create_index([("session_id", 1), ("created_at", -1)])
            await collection.create_index(
                [("session_id", 1), ("checkpoint_id", 1)], unique=True
            )
            self._indexes_ready = True
        self._collection = collection
        return collection

    def _uri(self, checkpoint_id: str) -> str:
        return f"mongodb://{self.database}/{self.collection_name}/{checkpoint_id}"

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        """Insert a new checkpoint document and return a populated :class:`CheckpointRef`."""
        import orjson

        collection = await self._ensure_collection()
        checkpoint_id = _new_ulid()
        payload_bytes = orjson.dumps(dict(state), default=str)
        created_at = datetime.now(UTC)
        await collection.insert_one(
            {
                "session_id": session_id,
                "checkpoint_id": checkpoint_id,
                "reason": reason,
                "created_at": created_at,
                "bytes": len(payload_bytes),
                "state": dict(state),
            }
        )
        return CheckpointRef(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            reason=reason,
            created_at=created_at,
            bytes=len(payload_bytes),
            uri=self._uri(checkpoint_id),
        )

    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the stored state for ``checkpoint_id`` (or the latest write)."""
        collection = await self._ensure_collection()
        if checkpoint_id is None:
            doc = await collection.find_one(
                {"session_id": session_id},
                sort=[("created_at", -1)],
            )
            if doc is None:
                return None
            return self._extract_state(doc)
        doc = await collection.find_one(
            {"session_id": session_id, "checkpoint_id": checkpoint_id}
        )
        if doc is None:
            raise CheckpointMissing(session_id, checkpoint_id)
        return self._extract_state(doc)

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        """Return checkpoint refs newest-first up to ``limit`` documents."""
        collection = await self._ensure_collection()
        cursor = (
            collection.find({"session_id": session_id}, {"state": 0})
            .sort("created_at", -1)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
        return [
            CheckpointRef(
                session_id=session_id,
                checkpoint_id=doc["checkpoint_id"],
                reason=doc.get("reason", "turn_end"),
                created_at=doc["created_at"],
                bytes=int(doc.get("bytes", 0)),
                uri=self._uri(doc["checkpoint_id"]),
            )
            for doc in docs
        ]

    async def delete_session(self, session_id: str) -> None:
        """Remove every document belonging to ``session_id`` (relies on doc atomicity)."""
        collection = await self._ensure_collection()
        await collection.delete_many({"session_id": session_id})

    @staticmethod
    def _extract_state(doc: Mapping[str, Any]) -> Mapping[str, Any]:
        state = doc.get("state")
        if not isinstance(state, Mapping):
            raise CheckpointerError(
                f"MongoCheckpointer document has non-Mapping state: {type(state).__name__}"
            )
        return dict(state)


__all__ = ["MongoCheckpointer"]
