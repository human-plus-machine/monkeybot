"""Firestore persistence for prompt-first scheduled agent loops."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from typing import TypeVar, cast

from google.cloud import firestore
from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from monkeybot.core.persistence.scheduled_loops import (
    GOAL_MAX_CONSECUTIVE_ERRORS,
    KIND_GOAL,
    KIND_LOOP,
    OPEN_GOAL_STATUSES,
    OpenGoalExistsError,
    PlannedScheduledLoop,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    doc_to_scheduled_loop_row,
    planned_create,
    resolve_complete_tick,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_LOOP_STATUSES = frozenset({"active", "paused", "completed", "failed"})
_STALE_RELEASE_MAX_PER_PASS = 400
_STALE_RELEASE_CONCURRENCY = 25


def _collection_name(prefix: str, base: str) -> str:
    if not prefix:
        return base
    return f"{prefix}_{base}"


def _validated_kind(kind: str) -> str:
    if kind not in {KIND_LOOP, KIND_GOAL}:
        raise ValueError(f"invalid scheduled loop kind: {kind!r}")
    return kind


def _create_payload(values: PlannedScheduledLoop) -> dict[str, object]:
    return {
        "session_id": values.session_id,
        "status": "active",
        "prompt": values.prompt,
        "interval_ms": values.interval_ms,
        "max_ticks": values.max_ticks,
        "max_runtime_ms": values.max_runtime_ms,
        "skip_if_busy": 1 if values.skip_if_busy else 0,
        "tick_index": 0,
        "next_tick_at_ms": values.next_tick_at_ms,
        "started_at_ms": values.started_at_ms,
        "last_tick_at_ms": None,
        "last_error": None,
        "stop_reason": None,
        "tick_in_flight": 0,
        "worker_id": None,
        "claimed_at_ms": None,
        "kind": values.kind,
        "objective": values.objective,
        "consecutive_error_count": 0,
    }


class FirestoreScheduledLoopStore:
    """Firestore-backed scheduled-loop store."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._collection = _collection_name(prefix, "scheduled_loops")

    def _doc(self, loop_id: str) -> firestore.AsyncDocumentReference:
        return self._client.collection(self._collection).document(loop_id)

    def _goal_lock_doc(self, session_id: str) -> firestore.AsyncDocumentReference:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self._client.collection(f"{self._collection}_goal_sessions").document(digest)

    @staticmethod
    def _try_doc_to_row(doc_id: str, data: dict[str, object]) -> ScheduledLoopRow | None:
        """Map a doc to a row, skipping (and logging) malformed documents.

        A single bad doc (e.g. ``interval_ms <= 0`` from legacy/hand-edited
        data) must not raise out of list_all/list_due and stall every other
        loop's poll cycle.
        """
        try:
            return doc_to_scheduled_loop_row(doc_id, data)
        except ValueError as exc:
            logger.error("skipping malformed scheduled loop doc_id=%s: %s", doc_id, exc)
            return None

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
        now_ms = int(time.time() * 1000)
        values = planned_create(spec, now_ms=now_ms)
        payload = _create_payload(values)
        if values.kind == KIND_GOAL:
            await self._insert_goal(values, payload)
        else:
            await self._insert_loop(values.loop_id, payload)
        row = await self.get(values.loop_id)
        if row is None:
            raise RuntimeError("failed to read scheduled loop after insert")
        return row

    async def _insert_loop(self, loop_id: str, payload: dict[str, object]) -> None:
        async def _create_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> None:
            snapshot = await ref.get(transaction=txn)
            if snapshot.exists:
                raise ValueError(f"scheduled loop already exists: {loop_id}")
            txn.set(ref, payload)

        await self._in_transaction(self._doc(loop_id), _create_body)

    async def _insert_goal(self, values: PlannedScheduledLoop, payload: dict[str, object]) -> None:
        """Write the goal and claim its session lock in one transaction.

        Firestore has no partial unique index, so a per-session lock document
        serializes concurrent creates. A lock pointing at a goal that is gone or
        already closed is stale and gets overwritten.
        """
        loop_id = values.loop_id
        doc_ref = self._doc(loop_id)
        lock_ref = self._goal_lock_doc(values.session_id)

        async def _create_goal_body(txn: firestore.AsyncTransaction) -> None:
            if (await doc_ref.get(transaction=txn)).exists:
                raise ValueError(f"scheduled loop already exists: {loop_id}")
            lock_data = (await lock_ref.get(transaction=txn)).to_dict() or {}
            open_goal_id = str(lock_data.get("goal_id", "")).strip()
            if open_goal_id and await self._is_open_goal(open_goal_id, txn=txn):
                raise OpenGoalExistsError(
                    f"an open goal already exists for session: {values.session_id}"
                )
            txn.set(doc_ref, payload)
            txn.set(
                lock_ref,
                {
                    "session_id": values.session_id,
                    "goal_id": loop_id,
                    "updated_at_ms": values.started_at_ms,
                },
            )

        transaction = self._client.transaction()
        create_goal = cast(
            Callable[[firestore.AsyncTransaction], Awaitable[None]],
            firestore.async_transactional(_create_goal_body),
        )
        await create_goal(transaction)

    async def _is_open_goal(self, goal_id: str, *, txn: firestore.AsyncTransaction) -> bool:
        snapshot = await self._doc(goal_id).get(transaction=txn)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        return data.get("kind") == KIND_GOAL and data.get("status") in OPEN_GOAL_STATUSES

    async def get(self, loop_id: str) -> ScheduledLoopRow | None:
        snapshot = await self._doc(loop_id).get()
        if not snapshot.exists:
            return None
        return self._try_doc_to_row(snapshot.id, snapshot.to_dict() or {})

    async def list_all(self) -> list[ScheduledLoopRow]:
        rows: list[ScheduledLoopRow] = []
        query = self._client.collection(self._collection).order_by(
            "started_at_ms", direction=firestore.Query.DESCENDING
        )
        async for doc in query.stream():
            row = self._try_doc_to_row(doc.id, doc.to_dict() or {})
            if row is not None:
                rows.append(row)
        return rows

    async def list_kind(self, kind: str) -> list[ScheduledLoopRow]:
        if _validated_kind(kind) == KIND_LOOP:
            # Docs written before `kind` existed carry no field to filter on,
            # so scan and let the row mapper default them to KIND_LOOP.
            return [row for row in await self.list_all() if row.kind == KIND_LOOP]
        rows: list[ScheduledLoopRow] = []
        query = self._client.collection(self._collection).where(
            filter=FieldFilter("kind", "==", kind)
        )
        async for doc in query.stream():
            row = self._try_doc_to_row(doc.id, doc.to_dict() or {})
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda item: item.started_at_ms, reverse=True)
        return rows

    async def find_open(
        self,
        *,
        session_id: str,
        kind: str,
        statuses: Collection[str],
    ) -> ScheduledLoopRow | None:
        wanted = {str(status) for status in statuses}
        if _validated_kind(kind) == KIND_LOOP:
            rows = await self.list_kind(KIND_LOOP)
            return next(
                (row for row in rows if row.session_id == session_id and row.status in wanted),
                None,
            )
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("kind", "==", kind))
            .where(filter=FieldFilter("session_id", "==", session_id))
        )
        async for doc in query.stream():
            row = self._try_doc_to_row(doc.id, doc.to_dict() or {})
            if row is not None and row.status in wanted:
                return row
        return None

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
            row = self._try_doc_to_row(doc.id, doc.to_dict() or {})
            if row is not None:
                rows.append(row)
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
            update: dict[str, object] = {
                "tick_in_flight": 0,
                "worker_id": None,
                "claimed_at_ms": None,
            }
            if data.get("kind") == KIND_GOAL:
                error_count = int(cast(int, data.get("consecutive_error_count", 0))) + 1
                update["last_error"] = "stale tick claim released"
                update["consecutive_error_count"] = error_count
                if error_count >= GOAL_MAX_CONSECUTIVE_ERRORS:
                    update["status"] = "failed"
                    update["stop_reason"] = "consecutive_tick_errors"
            else:
                update["last_error"] = data.get("last_error") or "stale tick claim released"
            txn.update(ref, update)
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
            if len(stale_loop_ids) >= _STALE_RELEASE_MAX_PER_PASS:
                break
        reset = 0
        for i in range(0, len(stale_loop_ids), _STALE_RELEASE_CONCURRENCY):
            chunk = stale_loop_ids[i : i + _STALE_RELEASE_CONCURRENCY]
            results = await asyncio.gather(
                *(self._release_one_stale_claim(loop_id, cutoff) for loop_id in chunk)
            )
            reset += sum(1 for ok in results if ok)
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
                logger.error("complete_tick skipped loop_id=%s: %s", loop_id, exc)
                return False
            result = resolve_complete_tick(row, error=error, now_ms=now_ms)
            txn.update(
                ref,
                {
                    "tick_index": result.tick_index,
                    "last_tick_at_ms": now_ms,
                    "last_error": result.last_error,
                    "status": result.status,
                    "stop_reason": result.stop_reason,
                    "next_tick_at_ms": result.next_tick_at_ms,
                    "consecutive_error_count": result.consecutive_error_count,
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
            if (
                data.get("worker_id") != worker_id
                or int(cast(int, data.get("tick_in_flight", 0))) != 1
            ):
                logger.warning(
                    "defer_tick skipped loop_id=%s worker_id=%s (claim lost or not in flight)",
                    loop_id,
                    worker_id,
                )
                return False
            try:
                row = doc_to_scheduled_loop_row(snapshot.id, data)
            except ValueError as exc:
                logger.error(
                    "defer_tick skipped loop_id=%s worker_id=%s: %s",
                    loop_id,
                    worker_id,
                    exc,
                )
                return False
            now_ms = int(time.time() * 1000)
            update: dict[str, object] = {
                "tick_in_flight": 0,
                "worker_id": None,
                "claimed_at_ms": None,
                "next_tick_at_ms": now_ms + row.interval_ms,
            }
            if row.kind != KIND_GOAL:
                update["last_error"] = reason
            txn.update(ref, update)
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

    async def set_status(
        self, loop_id: str, status: str, *, stop_reason: str | None = None
    ) -> bool:
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
