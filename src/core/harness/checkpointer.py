"""CheckpointerBackend protocol + in-memory default + Firestore wrapper.

.. deprecated::
    The classes in this module are superseded by the ABC-based backends in
    :mod:`src.core.harness.extensions.checkpointers`. They remain here for
    backward compatibility with pre-Story-2 consumers (they re-export from
    ``emonk.core.harness``). **New code should use
    :class:`src.core.harness.extensions.base.Checkpointer` subclasses.**

    The legacy ``Protocol`` returns the minimal :class:`CheckpointRef`
    (``id``/``session_id``/``ts``/``reason``). The new ABC returns the richer
    :class:`src.core.harness.extensions.values.CheckpointRef`
    (``session_id``/``checkpoint_id``/``created_at``/``bytes``/``uri``). The
    assembler keeps using the legacy protocol for the zero-change default
    path (``cfg.scheduler.storage == "firestore"``) so existing bots continue
    to work without opting into the new ABC.

    The ``DynamoDBCheckpointerStub`` that used to live here was removed —
    Story 9 ships a worked DynamoDB example via the new ABC.
"""

from __future__ import annotations

import pickle
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class CheckpointRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    session_id: str
    ts: datetime
    reason: str


@runtime_checkable
class CheckpointerBackend(Protocol):
    async def write(self, session_id: str, state: Any, *, reason: str) -> CheckpointRef: ...
    async def read(self, session_id: str, checkpoint_id: str | None = None) -> Any: ...
    async def list(self, session_id: str) -> list[CheckpointRef]: ...
    async def delete_session(self, session_id: str) -> None: ...


@dataclass
class _Entry:
    ref: CheckpointRef
    payload: bytes


@dataclass
class InMemoryCheckpointer(CheckpointerBackend):
    _store: dict[str, list[_Entry]] = field(default_factory=dict)

    async def write(self, session_id: str, state: Any, *, reason: str) -> CheckpointRef:
        ref = CheckpointRef(
            id=f"ckpt_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            ts=datetime.now(UTC),
            reason=reason,
        )
        self._store.setdefault(session_id, []).append(_Entry(ref=ref, payload=pickle.dumps(state)))
        return ref

    async def read(self, session_id: str, checkpoint_id: str | None = None) -> Any:
        entries = self._store.get(session_id) or []
        if not entries:
            return None
        if checkpoint_id is None:
            return pickle.loads(entries[-1].payload)
        for e in entries:
            if e.ref.id == checkpoint_id:
                return pickle.loads(e.payload)
        return None

    async def list(self, session_id: str) -> list[CheckpointRef]:
        return [e.ref for e in self._store.get(session_id, [])]

    async def delete_session(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class FirestoreCheckpointer(CheckpointerBackend):
    """Legacy Firestore-backed :class:`CheckpointerBackend` (deprecated).

    .. deprecated::
        Prefer
        :class:`src.core.harness.extensions.checkpointers.firestore.FirestoreCheckpointer`
        (the ABC-conformant implementation shipped in Story 2). This class is
        kept as the zero-change default for legacy configs that set
        ``cfg.scheduler.storage == "firestore"``; a :class:`DeprecationWarning`
        is emitted on construction so consumers can migrate at their own pace.
    """

    def __init__(self, collection: str = "harness_checkpoints") -> None:
        warnings.warn(
            "src.core.harness.checkpointer.FirestoreCheckpointer is deprecated; "
            "use src.core.harness.extensions.checkpointers.firestore.FirestoreCheckpointer "
            "for new code (see docs/extending-the-harness.md).",
            DeprecationWarning,
            stacklevel=2,
        )
        self.collection = collection
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import firestore  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "FirestoreCheckpointer requires emonk[firestore]"
                ) from exc
            self._client = firestore.Client()
        return self._client

    async def write(self, session_id: str, state: Any, *, reason: str) -> CheckpointRef:
        import asyncio

        ref = CheckpointRef(
            id=f"ckpt_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            ts=datetime.now(UTC),
            reason=reason,
        )
        doc = (
            self._get_client()
            .collection(self.collection)
            .document(ref.id)
        )
        await asyncio.to_thread(
            doc.set,
            {
                "session_id": session_id,
                "ts": ref.ts,
                "reason": reason,
                "payload": pickle.dumps(state),
            },
        )
        return ref

    async def read(self, session_id: str, checkpoint_id: str | None = None) -> Any:
        import asyncio

        col = self._get_client().collection(self.collection)
        if checkpoint_id:
            snap = await asyncio.to_thread(col.document(checkpoint_id).get)
            data = snap.to_dict() if snap.exists else None
            if data is None or data.get("session_id") != session_id:
                return None
            return pickle.loads(data["payload"])
        docs = await asyncio.to_thread(
            lambda: list(
                col.where("session_id", "==", session_id)
                .order_by("ts", direction="DESCENDING")
                .limit(1)
                .stream()
            )
        )
        if not docs:
            return None
        return pickle.loads(docs[0].to_dict()["payload"])

    async def list(self, session_id: str) -> list[CheckpointRef]:
        import asyncio

        col = self._get_client().collection(self.collection)
        docs = await asyncio.to_thread(
            lambda: list(col.where("session_id", "==", session_id).stream())
        )
        refs = [
            CheckpointRef(
                id=d.id,
                session_id=d.to_dict()["session_id"],
                ts=d.to_dict()["ts"],
                reason=d.to_dict().get("reason", "unknown"),
            )
            for d in docs
        ]
        return sorted(refs, key=lambda r: r.ts)

    async def delete_session(self, session_id: str) -> None:
        import asyncio

        col = self._get_client().collection(self.collection)
        docs = await asyncio.to_thread(
            lambda: list(col.where("session_id", "==", session_id).stream())
        )
        for d in docs:
            await asyncio.to_thread(d.reference.delete)
