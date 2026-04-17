"""Firestore-backed :class:`Checkpointer` shipped as a builtin backend.

Subclasses the new ABC at :mod:`src.core.harness.extensions.base`. State is
serialised via :func:`pickle.dumps` and stored under the configured Firestore
collection. The Firestore client is lazy-loaded so importing this module does
not require ``google-cloud-firestore``.
"""

from __future__ import annotations

import asyncio
import itertools
import pickle
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from ..base import Checkpointer
from ..errors import CheckpointMissing
from ..values import CheckpointRef


class FirestoreCheckpointer(Checkpointer):
    """Firestore-backed ABC-conformant checkpointer.

    Documents live under ``{collection}/{checkpoint_id}`` with fields
    ``session_id``, ``checkpoint_id``, ``reason``, ``created_at``, ``bytes``
    and ``payload``. Reads filter by ``session_id`` and order by
    ``created_at DESC`` so list/latest semantics match the contract.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        collection: str = "checkpoints",
    ) -> None:
        self.project_id = project_id
        self.collection = collection
        self._client: Any = None
        self._counters: dict[str, itertools.count[int]] = {}
        self._counter_lock = asyncio.Lock()

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import firestore  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise RuntimeError(
                    "FirestoreCheckpointer requires emonk[firestore]"
                ) from exc
            self._client = (
                firestore.Client(project=self.project_id)
                if self.project_id
                else firestore.Client()
            )
        return self._client

    async def _next_id(self, session_id: str) -> str:
        async with self._counter_lock:
            counter = self._counters.setdefault(session_id, itertools.count(1))
            seq = next(counter)
        return f"{seq:016d}-{uuid.uuid4().hex[:8]}"

    def _uri(self, checkpoint_id: str) -> str:
        return f"firestore://{self.project_id or 'default'}/{self.collection}/{checkpoint_id}"

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        """Write ``state`` to Firestore and return a populated :class:`CheckpointRef`."""
        checkpoint_id = await self._next_id(session_id)
        payload = pickle.dumps(dict(state))
        created_at = datetime.now(UTC)
        client = self._get_client()
        doc = client.collection(self.collection).document(checkpoint_id)
        await asyncio.to_thread(
            doc.set,
            {
                "session_id": session_id,
                "checkpoint_id": checkpoint_id,
                "reason": reason,
                "created_at": created_at,
                "bytes": len(payload),
                "payload": payload,
            },
        )
        return CheckpointRef(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            reason=reason,
            created_at=created_at,
            bytes=len(payload),
            uri=self._uri(checkpoint_id),
        )

    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the stored payload for ``checkpoint_id`` or the latest write."""
        client = self._get_client()
        col = client.collection(self.collection)
        if checkpoint_id is not None:
            snap = await asyncio.to_thread(col.document(checkpoint_id).get)
            exists = getattr(snap, "exists", False)
            if not exists:
                raise CheckpointMissing(session_id, checkpoint_id)
            data = snap.to_dict() or {}
            if data.get("session_id") != session_id:
                raise CheckpointMissing(session_id, checkpoint_id)
            return self._deserialize(data["payload"])

        docs = await asyncio.to_thread(
            lambda: list(
                col.where("session_id", "==", session_id)
                .order_by("created_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
        )
        if not docs:
            return None
        return self._deserialize(docs[0].to_dict()["payload"])

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        """Return checkpoint refs newest-first for ``session_id``."""
        client = self._get_client()
        col = client.collection(self.collection)
        docs = await asyncio.to_thread(
            lambda: list(
                col.where("session_id", "==", session_id)
                .order_by("created_at", direction="DESCENDING")
                .limit(limit)
                .stream()
            )
        )
        refs: list[CheckpointRef] = []
        for doc in docs:
            data = doc.to_dict() or {}
            refs.append(
                CheckpointRef(
                    session_id=session_id,
                    checkpoint_id=data.get("checkpoint_id", doc.id),
                    reason=data.get("reason", "turn_end"),
                    created_at=data["created_at"],
                    bytes=int(data.get("bytes", 0)),
                    uri=self._uri(data.get("checkpoint_id", doc.id)),
                )
            )
        return refs

    async def delete_session(self, session_id: str) -> None:
        """Delete every Firestore document belonging to ``session_id``."""
        client = self._get_client()
        col = client.collection(self.collection)
        docs = await asyncio.to_thread(
            lambda: list(col.where("session_id", "==", session_id).stream())
        )
        for doc in docs:
            await asyncio.to_thread(doc.reference.delete)
        self._counters.pop(session_id, None)

    @staticmethod
    def _deserialize(payload: Any) -> Mapping[str, Any]:
        raw = payload if isinstance(payload, bytes | bytearray) else bytes(payload)
        result: Any = pickle.loads(raw)  # noqa: S301 - trusted in-process payload
        if not isinstance(result, Mapping):
            raise TypeError(
                f"FirestoreCheckpointer expected a Mapping payload, got {type(result).__name__}"
            )
        return result


__all__ = ["FirestoreCheckpointer"]
