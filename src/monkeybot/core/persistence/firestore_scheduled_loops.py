"""Firestore persistence for prompt-first scheduled agent loops."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from google.cloud import firestore
from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from monkeybot.core.persistence.scheduled_loops import (
    ScheduledLoopCreate,
    ScheduledLoopRow,
    _loop_id_from_create,
    doc_to_scheduled_loop_row,
    validate_loop_guards,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_LOOP_STATUSES = frozenset({"active", "paused", "completed", "failed"})


def _collection_name(prefix: str, base: str) -> str:
    if not prefix:
        return base
    return f"{prefix}_{base}"


class FirestoreScheduledLoopStore:
    """Firestore-backed scheduled-loop store."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._collection = _collection_name(prefix, "scheduled_loops")

    def _doc(self, loop_id: str) -> firestore.AsyncDocumentReference:
        return self._client.collection(self._collection).document(loop_id)

    async def _in_transaction(
        self,
        doc_ref: firestore.AsyncDocumentReference,
        body: Callable[
            [firestore.AsyncTransaction, firestore.AsyncDocumentReference],
            Awaitable[T],
        ],
    ) -> T:
        transaction = self._client.transaction()
        txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference],
                Awaitable[T],
            ],
            firestore.async_transactional(body),
        )
        return await txn(transaction, doc_ref)

    async def create(self, spec: ScheduledLoopCreate) -> ScheduledLoopRow:
        loop_id = _loop_id_from_create(spec)
        validate_loop_guards(
            max_ticks=spec.max_ticks,
            max_runtime_ms=spec.max_runtime_ms,
            unbounded=spec.unbounded,
        )
        now_ms = int(time.time() * 1000)
        payload = {
            "session_id": spec.session_id.strip() or "loop-main",
            "status": "active",
            "prompt": spec.prompt.strip(),
            "interval_ms": spec.interval_ms,
            "max_ticks": spec.max_ticks,
            "max_runtime_ms": spec.max_runtime_ms,
            "skip_if_busy": 1 if spec.skip_if_busy else 0,
            "tick_index": 0,
            "next_tick_at_ms": now_ms,
            "started_at_ms": now_ms,
            "last_tick_at_ms": None,
            "last_error": None,
            "stop_reason": None,
            "tick_in_flight": 0,
            "worker_id": None,
            "claimed_at_ms": None,
        }
        doc_ref = self._doc(loop_id)

        async def _create_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> None:
            snapshot = await ref.get(transaction=txn)
            if snapshot.exists:
                raise ValueError(f"scheduled loop already exists: {loop_id}")
            txn.set(ref, payload)

        await self._in_transaction(doc_ref, _create_body)
        row = await self.get(loop_id)
        if row is None:
            raise RuntimeError("failed to read scheduled loop after insert")
        return row

    async def get(self, loop_id: str) -> ScheduledLoopRow | None:
        snapshot = await self._doc(loop_id).get()
        if not snapshot.exists:
            return None
        return doc_to_scheduled_loop_row(snapshot.id, snapshot.to_dict() or {})

    async def list_all(self) -> list[ScheduledLoopRow]:
        rows: list[ScheduledLoopRow] = []
        query = self._client.collection(self._collection).order_by(
            "started_at_ms", direction=firestore.Query.DESCENDING
        )
        async for doc in query.stream():
            rows.append(doc_to_scheduled_loop_row(doc.id, doc.to_dict() or {}))
        return rows

    async def list_due(self, now_ms: int) -> list[ScheduledLoopRow]:
        rows: list[ScheduledLoopRow] = []
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("status", "==", "active"))
            .where(filter=FieldFilter("tick_in_flight", "==", 0))
            .where(filter=FieldFilter("next_tick_at_ms", "<=", now_ms))
            .order_by("next_tick_at_ms")
        )
        async for doc in query.stream():
            rows.append(doc_to_scheduled_loop_row(doc.id, doc.to_dict() or {}))
        return rows

    async def claim_tick(self, loop_id: str, worker_id: str) -> ScheduledLoopRow | None:
        doc_ref = self._doc(loop_id)
        now_ms = int(time.time() * 1000)

        async def _claim_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("status") != "active":
                return False
            if int(cast(int, data.get("tick_in_flight", 0))) != 0:
                return False
            if int(cast(int, data.get("next_tick_at_ms", 0))) > now_ms:
                return False
            txn.update(
                ref,
                {
                    "tick_in_flight": 1,
                    "worker_id": worker_id,
                    "claimed_at_ms": now_ms,
                },
            )
            return True

        if not await self._in_transaction(doc_ref, _claim_body):
            return None
        return await self.get(loop_id)

    async def _release_one_stale_claim(self, loop_id: str, cutoff: int) -> bool:
        """Transactionally clear one stale claim; no-op if heartbeat renewed past cutoff."""
        doc_ref = self._doc(loop_id)

        async def _release_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if int(cast(int, data.get("tick_in_flight", 0))) != 1:
                return False
            claimed_at = data.get("claimed_at_ms")
            if claimed_at is None or int(cast(int, claimed_at)) >= cutoff:
                return False
            txn.update(
                ref,
                {
                    "tick_in_flight": 0,
                    "worker_id": None,
                    "claimed_at_ms": None,
                    "last_error": data.get("last_error") or "stale tick claim released",
                },
            )
            return True

        return bool(await self._in_transaction(doc_ref, _release_body))

    async def release_stale_claims(self, stale_after_ms: int) -> int:
        cutoff = int(time.time() * 1000) - stale_after_ms
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("tick_in_flight", "==", 1))
            .where(filter=FieldFilter("claimed_at_ms", "<", cutoff))
        )
        stale_loop_ids: list[str] = []
        async for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("claimed_at_ms") is None:
                continue
            stale_loop_ids.append(doc.id)
        reset = 0
        for loop_id in stale_loop_ids:
            if await self._release_one_stale_claim(loop_id, cutoff):
                reset += 1
        return reset

    async def complete_tick(
        self,
        loop_id: str,
        *,
        worker_id: str,
        error: str | None = None,
    ) -> ScheduledLoopRow | None:
        doc_ref = self._doc(loop_id)
        now_ms = int(time.time() * 1000)

        async def _complete_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("worker_id") != worker_id or int(data.get("tick_in_flight", 0)) != 1:
                return False
            try:
                row = doc_to_scheduled_loop_row(snapshot.id, data)
            except ValueError as exc:
                logger.error("%s", exc)
                return False
            tick_index = row.tick_index + 1
            status = row.status
            stop_reason: str | None = None
            started_at_ms = row.started_at_ms
            if error:
                status = "failed"
                stop_reason = "tick_error"
            elif row.max_ticks is not None and tick_index >= row.max_ticks:
                status = "completed"
                stop_reason = "max_ticks"
            elif (
                row.max_runtime_ms is not None
                and (now_ms - started_at_ms) >= row.max_runtime_ms
            ):
                status = "completed"
                stop_reason = "max_runtime"
            next_tick_at = (
                now_ms + row.interval_ms
                if status == "active"
                else row.next_tick_at_ms
            )
            txn.update(
                ref,
                {
                    "tick_index": tick_index,
                    "last_tick_at_ms": now_ms,
                    "last_error": error,
                    "status": status,
                    "stop_reason": stop_reason,
                    "next_tick_at_ms": next_tick_at,
                    "tick_in_flight": 0,
                    "worker_id": None,
                    "claimed_at_ms": None,
                },
            )
            return True

        if not await self._in_transaction(doc_ref, _complete_body):
            return None
        return await self.get(loop_id)

    async def defer_tick(self, loop_id: str, *, worker_id: str, reason: str) -> bool:
        """Release claim and push next tick forward (e.g. session busy).

        Must re-check ``worker_id`` + ``tick_in_flight`` inside a transaction —
        a stale-release + reclaim race can otherwise clear another worker's claim.
        """
        doc_ref = self._doc(loop_id)

        async def _defer_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("worker_id") != worker_id or int(cast(int, data.get("tick_in_flight", 0))) != 1:
                logger.warning(
                    "defer_tick skipped loop_id=%s worker_id=%s (claim lost or not in flight)",
                    loop_id,
                    worker_id,
                )
                return False
            try:
                row = doc_to_scheduled_loop_row(snapshot.id, data)
            except ValueError as exc:
                logger.error("%s", exc)
                return False
            now_ms = int(time.time() * 1000)
            txn.update(
                ref,
                {
                    "tick_in_flight": 0,
                    "worker_id": None,
                    "claimed_at_ms": None,
                    "next_tick_at_ms": now_ms + row.interval_ms,
                    "last_error": reason,
                },
            )
            return True

        return bool(await self._in_transaction(doc_ref, _defer_body))

    async def renew_tick_claim(self, loop_id: str, worker_id: str) -> bool:
        doc_ref = self._doc(loop_id)
        now_ms = int(time.time() * 1000)

        async def _renew_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("worker_id") != worker_id or int(data.get("tick_in_flight", 0)) != 1:
                return False
            txn.update(ref, {"claimed_at_ms": now_ms})
            return True

        return bool(await self._in_transaction(doc_ref, _renew_body))

    async def set_status(self, loop_id: str, status: str, *, stop_reason: str | None = None) -> bool:
        if status not in _LOOP_STATUSES:
            raise ValueError(f"invalid loop status: {status}")
        snapshot = await self._doc(loop_id).get()
        if not snapshot.exists:
            return False
        payload: dict[str, object] = {
            "status": status,
            "tick_in_flight": 0,
            "worker_id": None,
            "claimed_at_ms": None,
        }
        if stop_reason is not None:
            payload["stop_reason"] = stop_reason
        await self._doc(loop_id).update(payload)
        return True

    async def pause(self, loop_id: str) -> bool:
        return await self.set_status(loop_id, "paused")

    async def resume(self, loop_id: str) -> bool:
        snapshot = await self._doc(loop_id).get()
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if data.get("status") != "paused":
            return False
        now_ms = int(time.time() * 1000)
        await self._doc(loop_id).update(
            {
                "status": "active",
                "stop_reason": None,
                "next_tick_at_ms": now_ms,
                "tick_in_flight": 0,
                "worker_id": None,
                "claimed_at_ms": None,
            }
        )
        return True

    async def stop(self, loop_id: str, *, stop_reason: str = "manual") -> bool:
        return await self.set_status(loop_id, "completed", stop_reason=stop_reason)
