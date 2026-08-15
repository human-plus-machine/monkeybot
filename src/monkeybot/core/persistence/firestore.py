"""Firestore storage backend (requires ``pip install 'monkeybot[firestore]'``)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore
from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.llm.usage import Usage, UsageBreakdown, UsageBucket, UsageSummary
from monkeybot.core.memory.ids import outbox_id, utc_now_iso
from monkeybot.core.memory.outbox import (
    STATUS_COMMITTED,
    STATUS_DEAD,
    STATUS_PENDING,
    OutboxRow,
    backoff_iso,
    is_permanent_error,
)
from monkeybot.core.persistence.backends import FirestoreConfig
from monkeybot.core.persistence.durable_runs import (
    SubagentEnvelope,
    SubagentRunRow,
)
from monkeybot.core.persistence.errors import AmbiguousCommitError
from monkeybot.core.persistence.firestore_scheduled_loops import FirestoreScheduledLoopStore
from monkeybot.core.persistence.thread_summary import ChatThreadSummary, preview_from_content_blob
from monkeybot.core.types.content_blocks import ContentBlock

logger = logging.getLogger(__name__)

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")


def _collection_name(prefix: str, base: str) -> str:
    if not prefix:
        return base
    return f"{prefix}_{base}"


def _memory_outbox_collection(client: AsyncClient, prefix: str) -> Any:
    """Stable collection ID ``memory_outbox`` so shipped indexes apply to every prefix.

    Path: ``{prefix or _default}/outbox/memory_outbox/{row_id}``.
    """
    parent = prefix.strip() or "_default"
    return client.collection(parent).document("outbox").collection("memory_outbox")


def _history_document_id(message_id: str) -> str:
    """Stable Firestore document ID for idempotent message delivery."""
    return "message_" + hashlib.sha256(message_id.encode()).hexdigest()


def _int_value(raw: object, default: int = 0) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def _float_value(raw: object, default: float = 0.0) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return default
    return default


def _field_int(row: dict[str, object], key: str, default: int = 0) -> int:
    return _int_value(row.get(key, default), default)


def _field_float(row: dict[str, object], key: str, default: float = 0.0) -> float:
    return _float_value(row.get(key, default), default)


def _history_row(
    thread_id: str,
    role: str,
    payload: str,
    created_at: int,
    turn_id: str | None,
    message_id: str | None,
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "role": role,
        "content": payload,
        "created_at": created_at,
        "turn_id": turn_id,
        "message_id": message_id,
    }


def _thread_summary_update(thread_id: str, created_at: int, payload: str) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "last_message_at": created_at,
        "message_count": firestore.Increment(1),
        "last_content": payload,
    }


class FirestoreHistoryStore:
    """Firestore-backed conversation history store."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._prefix = prefix
        self._collection = _collection_name(prefix, "conversation_history")
        self._threads_collection = _collection_name(prefix, "threads")

    async def _upsert_thread_summary(
        self,
        thread_id: str,
        *,
        created_at: int,
        content: str,
    ) -> None:
        """Maintain one summary doc per thread for indexed ``list_threads`` reads."""
        thread_ref = self._client.collection(self._threads_collection).document(thread_id)
        await thread_ref.set(
            _thread_summary_update(thread_id, created_at, content),
            merge=True,
        )

    async def _delete_thread_summary(self, thread_id: str) -> None:
        await self._client.collection(self._threads_collection).document(thread_id).delete()

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        role = message.role
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        history_data = _history_row(thread_id, role, payload, created_at, turn_id, message_id)
        if not message_id:
            await self._client.collection(self._collection).add(history_data)
            await self._upsert_thread_summary(thread_id, created_at=created_at, content=payload)
            return

        existing_query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("message_id", "==", message_id))
            .limit(1)
        )
        async for _existing in existing_query.stream():
            return
        hist_ref = self._client.collection(self._collection).document(
            _history_document_id(message_id)
        )
        thread_ref = self._client.collection(self._threads_collection).document(thread_id)
        summary_fields = _thread_summary_update(thread_id, created_at, payload)

        async def _append_body(txn: firestore.AsyncTransaction) -> None:
            snapshot = await hist_ref.get(transaction=txn)
            if snapshot.exists:
                return
            txn.set(hist_ref, history_data)
            txn.set(thread_ref, summary_fields, merge=True)

        append_txn = cast(
            Callable[[firestore.AsyncTransaction], Awaitable[None]],
            firestore.async_transactional(_append_body),
        )
        await append_txn(self._client.transaction())

    async def append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        """Insert history and a pending memory outbox row in one transaction."""
        role = message.role
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        row_id = outbox_id(
            agent_id=str(outbox.get("agent_id") or ""),
            thread_id=str(outbox.get("thread_id") or thread_id),
            message_id=str(outbox.get("message_id") or message_id),
            role=str(outbox.get("role") or role),
        )
        history_data = _history_row(thread_id, role, payload, created_at, turn_id, message_id)
        outbox_data = {
            "id": row_id,
            "agent_id": str(outbox.get("agent_id") or ""),
            "thread_id": thread_id,
            "turn_id": turn_id,
            "message_id": message_id,
            "role": role,
            "content": outbox.get("content"),
            "workspace_id": outbox.get("workspace_id"),
            "wing": outbox.get("wing") or "main",
            "room": outbox.get("room") or "conversation",
            "created_at": outbox.get("created_at") or utc_now_iso(),
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
            "traceparent": outbox.get("traceparent"),
            "lease_owner": None,
            "lease_expires_at": None,
            "palace_id": str(outbox.get("palace_id") or ""),
        }
        legacy_history_exists = False
        existing_query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("message_id", "==", message_id))
            .limit(1)
        )
        async for _existing in existing_query.stream():
            legacy_history_exists = True
            break

        hist_ref = self._client.collection(self._collection).document(
            _history_document_id(message_id)
        )
        thread_ref = self._client.collection(self._threads_collection).document(thread_id)
        summary_fields = _thread_summary_update(thread_id, created_at, payload)
        outbox_ref = _memory_outbox_collection(self._client, self._prefix).document(row_id)

        async def _append_body(txn: firestore.AsyncTransaction) -> None:
            history_snapshot = await hist_ref.get(transaction=txn)
            outbox_snapshot = await outbox_ref.get(transaction=txn)
            if not legacy_history_exists and not history_snapshot.exists:
                txn.set(hist_ref, history_data)
                txn.set(thread_ref, summary_fields, merge=True)
            if not outbox_snapshot.exists:
                txn.set(outbox_ref, outbox_data)

        append_txn = cast(
            Callable[[firestore.AsyncTransaction], Awaitable[None]],
            firestore.async_transactional(_append_body),
        )
        try:
            await append_txn(self._client.transaction())
        except (
            TimeoutError,
            OSError,
            ConnectionError,
            google_exceptions.DeadlineExceeded,
            google_exceptions.ServiceUnavailable,
            google_exceptions.InternalServerError,
            google_exceptions.Aborted,
            google_exceptions.Unknown,
        ) as extra:
            raise AmbiguousCommitError(str(extra)) from extra

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        if limit is not None:
            # Newest ``limit`` rows: query descending then reverse to chronological.
            query = (
                self._client.collection(self._collection)
                .where(filter=FieldFilter("thread_id", "==", thread_id))
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            rows = [doc async for doc in query.stream()]
            rows_chrono = list(reversed(rows))
        else:
            query = (
                self._client.collection(self._collection)
                .where(filter=FieldFilter("thread_id", "==", thread_id))
                .order_by("created_at", direction=firestore.Query.ASCENDING)
            )
            rows = [doc async for doc in query.stream()]
            rows_chrono = list(rows)
        out: list[Message] = []
        for doc in rows_chrono:
            data = doc.to_dict() or {}
            row_id = doc.id
            role = str(data.get("role", ""))
            content_blob = str(data.get("content", ""))
            try:
                raw = json.loads(content_blob)
                if not isinstance(raw, list):
                    raise ValueError("stored content must be a JSON array")
                blocks = [ContentBlock.from_dict(b) for b in raw]
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.error(
                    "Skipping unparseable history row id=%s thread_id=%s: %s",
                    row_id,
                    thread_id,
                    exc,
                    exc_info=True,
                )
                continue
            if role not in _VALID_ROLES:
                logger.error(
                    "Skipping history row id=%s thread_id=%s with invalid role=%r",
                    row_id,
                    thread_id,
                    role,
                )
                continue
            out.append(Message(role=cast(Role, role), content=blocks))
        return out

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        query = self._client.collection(self._collection).where(
            filter=FieldFilter("thread_id", "==", thread_id)
        )
        batch = self._client.batch()
        count = 0
        async for doc in query.stream():
            batch.delete(doc.reference)
            count += 1
            if count >= 400:
                await batch.commit()
                batch = self._client.batch()
                count = 0
        if count:
            await batch.commit()
        await self._delete_thread_summary(thread_id)
        for msg in messages:
            await self.append(thread_id, msg)

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        cap = max(1, min(limit, 200))
        query = (
            self._client.collection(self._threads_collection)
            .order_by("last_message_at", direction=firestore.Query.DESCENDING)
            .limit(cap)
        )
        out: list[ChatThreadSummary] = []
        async for doc in query.stream():
            data = doc.to_dict() or {}
            thread_id = str(data.get("thread_id") or doc.id)
            preview = preview_from_content_blob(str(data.get("last_content", "")))
            out.append(
                ChatThreadSummary(
                    thread_id=thread_id,
                    last_message_at=_field_int(data, "last_message_at"),
                    message_count=_field_int(data, "message_count"),
                    preview=preview or "(empty)",
                )
            )
        return out


class FirestoreUsageStore:
    """Firestore-backed usage store."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._collection = _collection_name(prefix, "turn_usage")

    async def record(
        self,
        thread_id: str,
        model: str,
        usage: Usage,
        run_id: str | None = None,
        *,
        context_json: str | None = None,
    ) -> None:
        now_ms = int(time.time() * 1000)
        await self._client.collection(self._collection).add(
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "model": model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens,
                "cost_usd": usage.cost_usd,
                "duration_ms": usage.duration_ms,
                "created_at": now_ms,
                "context_json": context_json,
                "estimated_prompt_tokens": usage.estimated_prompt_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
            }
        )

    async def _fetch_usage_rows(
        self,
        thread_id: str | None,
        since_ms: int | None,
    ) -> list[dict[str, object]]:
        collection = self._client.collection(self._collection)
        if thread_id is None:
            doc_stream = collection.stream()
        else:
            doc_stream = collection.where(filter=FieldFilter("thread_id", "==", thread_id)).stream()
        rows: list[dict[str, object]] = []
        async for doc in doc_stream:
            data = doc.to_dict() or {}
            created_at = _field_int(data, "created_at")
            if since_ms is not None and created_at < since_ms:
                continue
            rows.append(data)
        return rows

    async def summary(
        self,
        thread_id: str | None = None,
        since_ms: int | None = None,
    ) -> UsageSummary:
        """Aggregate usage rows.

        When ``thread_id`` is ``None``, streams the entire ``turn_usage`` collection
        (small-scale only; not suitable for large production datasets).
        """
        rows = await self._fetch_usage_rows(thread_id, since_ms)
        if not rows:
            return UsageSummary(
                turns=0,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                cost_usd=0.0,
                period_start_ms=None,
                period_end_ms=None,
                last_prompt_tokens=0,
                last_estimated_prompt_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
            )

        input_tokens = sum(_field_int(r, "input_tokens") for r in rows)
        output_tokens = sum(_field_int(r, "output_tokens") for r in rows)
        cached_tokens = sum(_field_int(r, "cached_tokens") for r in rows)
        cost_usd = sum(_field_float(r, "cost_usd") for r in rows)
        cache_read_tokens = sum(_field_int(r, "cache_read_tokens") for r in rows)
        cache_creation_tokens = sum(_field_int(r, "cache_creation_tokens") for r in rows)
        created_times = [_field_int(r, "created_at") for r in rows]

        last_pt = 0
        last_est = 0
        if thread_id is not None:
            latest = max(rows, key=lambda r: _field_int(r, "created_at"))
            last_pt = _field_int(latest, "input_tokens")
            last_est = _field_int(latest, "estimated_prompt_tokens")

        return UsageSummary(
            turns=len(rows),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
            period_start_ms=min(created_times),
            period_end_ms=max(created_times),
            last_prompt_tokens=last_pt,
            last_estimated_prompt_tokens=last_est,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

    async def breakdown(self, since_ms: int | None = None) -> UsageBreakdown:
        """Aggregate usage by model and by UTC calendar day (in-process).

        When filtering with no thread, streams the entire ``turn_usage`` collection
        (small-scale only; not suitable for large production datasets).
        """
        rows = await self._fetch_usage_rows(None, since_ms)
        if not rows:
            return UsageBreakdown(by_model=[], by_day=[])

        by_model_map: dict[str, list[dict[str, object]]] = {}
        by_day_map: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            model = str(row.get("model") or "unknown")
            by_model_map.setdefault(model, []).append(row)
            day = time.strftime("%Y-%m-%d", time.gmtime(_field_int(row, "created_at") / 1000.0))
            by_day_map.setdefault(day, []).append(row)

        def _bucket(key: str, group: list[dict[str, object]]) -> UsageBucket:
            return UsageBucket(
                key=key,
                turns=len(group),
                input_tokens=sum(_field_int(r, "input_tokens") for r in group),
                output_tokens=sum(_field_int(r, "output_tokens") for r in group),
                cost_usd=sum(_field_float(r, "cost_usd") for r in group),
            )

        by_model = [_bucket(k, g) for k, g in by_model_map.items()]
        by_model.sort(key=lambda b: (-b.cost_usd, b.key))
        by_day = [_bucket(k, g) for k, g in sorted(by_day_map.items())]
        return UsageBreakdown(by_model=by_model, by_day=by_day)


def _doc_to_run_row(doc_id: str, data: dict[str, object]) -> SubagentRunRow:
    parent = data.get("parent_run_id")
    result = data.get("result_json")
    error = data.get("error_json")
    finished: int | None = data.get("finished_at")  # type: ignore[assignment]
    worker = data.get("worker_id")
    claimed: int | None = data.get("claimed_at")  # type: ignore[assignment]
    return SubagentRunRow(
        run_id=doc_id,
        parent_run_id=str(parent) if parent is not None else None,
        script=str(data.get("script", "")),
        envelope_json=str(data.get("envelope_json", "")),
        status=str(data.get("status", "")),
        result_json=str(result) if result is not None else None,
        error_json=str(error) if error is not None else None,
        started_at=_field_int(data, "started_at"),
        finished_at=int(finished) if finished is not None else None,
        scratch_dir=str(data.get("scratch_dir", "")),
        worker_id=str(worker) if worker is not None else None,
        claimed_at=int(claimed) if claimed is not None else None,
    )


class FirestoreRunStore:
    """Firestore-backed subagent run lifecycle store."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._collection = _collection_name(prefix, "subagent_runs")

    def _doc(self, run_id: str) -> firestore.AsyncDocumentReference:
        return self._client.collection(self._collection).document(run_id)

    async def record_pending(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: object,
    ) -> None:
        await self._record_run("pending", run_id, parent_run_id, script, envelope, scratch_dir)

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: object,
    ) -> None:
        await self._record_run("running", run_id, parent_run_id, script, envelope, scratch_dir)

    async def _record_run(
        self,
        status: str,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: object,
    ) -> None:
        now_ms = int(time.time() * 1000)
        await self._doc(run_id).set(
            {
                "parent_run_id": parent_run_id,
                "script": script,
                "envelope_json": envelope.to_json(),
                "status": status,
                "result_json": None,
                "error_json": None,
                "started_at": now_ms,
                "finished_at": None,
                "scratch_dir": str(scratch_dir),
                "worker_id": None,
                "claimed_at": None,
            }
        )

    async def claim(self, run_id: str, worker_id: str) -> bool:
        doc_ref = self._doc(run_id)
        transaction = self._client.transaction()

        async def _claim_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("status") != "pending":
                return False
            now_ms = int(time.time() * 1000)
            txn.update(
                ref,
                {
                    "status": "running",
                    "worker_id": worker_id,
                    "claimed_at": now_ms,
                },
            )
            return True

        claim_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]
            ],
            firestore.async_transactional(_claim_body),
        )
        return bool(await claim_txn(transaction, doc_ref))

    async def renew_claim(self, run_id: str, worker_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        doc_ref = self._doc(run_id)
        transaction = self._client.transaction()

        async def _renew_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snap = await ref.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("status") != "running" or data.get("worker_id") != worker_id:
                return False
            txn.update(ref, {"claimed_at": now_ms})
            return True

        renew_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]
            ],
            firestore.async_transactional(_renew_body),
        )
        return bool(await renew_txn(transaction, doc_ref))

    async def list_stale_claims(self, stale_after_ms: int) -> list[SubagentRunRow]:
        cutoff = int(time.time() * 1000) - stale_after_ms
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("status", "==", "running"))
            .where(filter=FieldFilter("claimed_at", "<", cutoff))
        )
        out: list[SubagentRunRow] = []
        async for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("claimed_at") is None:
                continue
            out.append(_doc_to_run_row(doc.id, data))
        return out

    async def reset_stale_claim(
        self,
        run_id: str,
        stale_after_ms: int,
        *,
        worker_id: str | None = None,
    ) -> bool:
        cutoff = int(time.time() * 1000) - stale_after_ms
        doc_ref = self._doc(run_id)
        transaction = self._client.transaction()

        async def _reset_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snap = await ref.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("status") != "running":
                return False
            claimed_at = data.get("claimed_at")
            if claimed_at is None or int(claimed_at) >= cutoff:
                return False
            if worker_id is not None and data.get("worker_id") != worker_id:
                return False
            txn.update(
                ref,
                {"status": "pending", "worker_id": None, "claimed_at": None},
            )
            return True

        reset_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]
            ],
            firestore.async_transactional(_reset_body),
        )
        return bool(await reset_txn(transaction, doc_ref))

    async def reset_stale_claims(self, stale_after_ms: int) -> int:
        cutoff = int(time.time() * 1000) - stale_after_ms
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("status", "==", "running"))
            .where(filter=FieldFilter("claimed_at", "<", cutoff))
        )
        reset = 0
        batch = self._client.batch()
        count = 0
        async for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("claimed_at") is None:
                continue
            batch.update(
                doc.reference,
                {"status": "pending", "worker_id": None, "claimed_at": None},
            )
            reset += 1
            count += 1
            if count >= 400:
                await batch.commit()
                batch = self._client.batch()
                count = 0
        if count:
            await batch.commit()
        return reset

    async def record_completed(
        self,
        run_id: str,
        result_json: str,
        *,
        worker_id: str | None = None,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        fields = {
            "status": "completed",
            "result_json": result_json,
            "finished_at": now_ms,
            "error_json": None,
        }
        if worker_id is None:
            await self._doc(run_id).update(fields)
            return True

        doc_ref = self._doc(run_id)
        transaction = self._client.transaction()

        async def _complete_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snap = await ref.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("status") != "running" or data.get("worker_id") != worker_id:
                return False
            txn.update(ref, fields)
            return True

        complete_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]
            ],
            firestore.async_transactional(_complete_body),
        )
        return bool(await complete_txn(transaction, doc_ref))

    async def record_failed(
        self,
        run_id: str,
        error: str,
        *,
        worker_id: str | None = None,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        err_payload = json.dumps({"message": error})
        fields = {
            "status": "failed",
            "error_json": err_payload,
            "finished_at": now_ms,
        }
        if worker_id is None:
            await self._doc(run_id).update(fields)
            return True

        doc_ref = self._doc(run_id)
        transaction = self._client.transaction()

        async def _fail_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snap = await ref.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("status") != "running" or data.get("worker_id") != worker_id:
                return False
            txn.update(ref, fields)
            return True

        fail_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]
            ],
            firestore.async_transactional(_fail_body),
        )
        return bool(await fail_txn(transaction, doc_ref))

    async def pending_runs(self) -> list[SubagentRunRow]:
        rows: list[SubagentRunRow] = []
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("status", "==", "pending"))
            .order_by("started_at")
        )
        async for doc in query.stream():
            rows.append(_doc_to_run_row(doc.id, doc.to_dict() or {}))
        return rows

    async def get_run(self, run_id: str) -> SubagentRunRow | None:
        snapshot = await self._doc(run_id).get()
        if not snapshot.exists:
            return None
        return _doc_to_run_row(snapshot.id, snapshot.to_dict() or {})


def _turn_lock_collection(prefix: str) -> str:
    if not prefix:
        return "session_turn_locks"
    return f"{prefix}_session_turn_locks"


class FirestoreSessionTurnLockStore:
    """Firestore-backed exclusive turn lock per session."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._collection = _turn_lock_collection(prefix)

    def _doc(self, session_id: str) -> firestore.AsyncDocumentReference:
        return self._client.collection(self._collection).document(session_id)

    async def release_stale_claims(self, stale_after_ms: int) -> int:
        cutoff = int(time.time() * 1000) - stale_after_ms
        query = self._client.collection(self._collection).where(
            filter=FieldFilter("claimed_at_ms", "<", cutoff)
        )
        reset = 0
        batch = self._client.batch()
        count = 0
        async for doc in query.stream():
            data = doc.to_dict() or {}
            if data.get("request_id") is None:
                continue
            batch.update(doc.reference, {"request_id": None, "claimed_at_ms": None})
            reset += 1
            count += 1
            if count >= 400:
                await batch.commit()
                batch = self._client.batch()
                count = 0
        if count:
            await batch.commit()
        return reset

    async def try_acquire(self, session_id: str, request_id: str) -> bool:
        from monkeybot.core.persistence.session_turn_locks import session_turn_stale_ms

        stale_ms = session_turn_stale_ms()
        doc_ref = self._doc(session_id)
        transaction = self._client.transaction()
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - stale_ms

        async def _claim_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                txn.set(ref, {"request_id": request_id, "claimed_at_ms": now_ms})
                return True
            data = snapshot.to_dict() or {}
            current = data.get("request_id")
            claimed_at = data.get("claimed_at_ms")
            if current is None:
                txn.update(ref, {"request_id": request_id, "claimed_at_ms": now_ms})
                return True
            if isinstance(claimed_at, int) and claimed_at < cutoff:
                txn.update(ref, {"request_id": request_id, "claimed_at_ms": now_ms})
                return True
            return False

        claim_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]
            ],
            firestore.async_transactional(_claim_body),
        )
        return bool(await claim_txn(transaction, doc_ref))

    async def release(self, session_id: str, request_id: str) -> None:
        doc_ref = self._doc(session_id)
        transaction = self._client.transaction()

        async def _release_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> None:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return
            data = snapshot.to_dict() or {}
            if data.get("request_id") != request_id:
                return
            txn.update(ref, {"request_id": None, "claimed_at_ms": None})

        release_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[None]
            ],
            firestore.async_transactional(_release_body),
        )
        await release_txn(transaction, doc_ref)

    async def is_busy(self, session_id: str) -> bool:
        snapshot = await self._doc(session_id).get()
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        return data.get("request_id") is not None


class FirestoreOutboxStore:
    """Firestore-backed memory outbox (one document per row)."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._col = _memory_outbox_collection(client, prefix)

    def _ref(self, row_id: str) -> Any:
        return self._col.document(row_id)

    def _row_from_data(self, row_id: str, data: dict[str, Any]) -> Any:
        return OutboxRow(
            id=row_id,
            thread_id=str(data.get("thread_id") or ""),
            turn_id=str(data.get("turn_id") or ""),
            message_id=str(data.get("message_id") or ""),
            role=str(data.get("role") or ""),
            content=data.get("content"),
            workspace_id=data.get("workspace_id"),
            wing=str(data.get("wing") or "main"),
            room=str(data.get("room") or "conversation"),
            created_at=str(data.get("created_at") or ""),
            status=str(data.get("status") or "pending"),
            attempts=int(data.get("attempts") or 0),
            next_attempt_at=data.get("next_attempt_at"),
            last_error=data.get("last_error"),
            traceparent=data.get("traceparent"),
            lease_owner=data.get("lease_owner"),
            lease_expires_at=data.get("lease_expires_at"),
            agent_id=str(data.get("agent_id") or ""),
            palace_id=str(data.get("palace_id") or ""),
        )

    async def insert_pending(
        self,
        *,
        agent_id: str,
        thread_id: str,
        turn_id: str,
        message_id: str,
        role: str,
        content: str,
        workspace_id: str | None,
        wing: str,
        room: str,
        created_at: str | None = None,
        traceparent: str | None = None,
        palace_id: str = "",
        commit: bool = True,
    ) -> str | None:
        del commit
        row_id = outbox_id(agent_id=agent_id, thread_id=thread_id, message_id=message_id, role=role)
        snap = await self._ref(row_id).get()
        if snap.exists:
            data = snap.to_dict() or {}
            return None if str(data.get("status")) == STATUS_COMMITTED else row_id
        await self._ref(row_id).set(
            {
                "id": row_id,
                "agent_id": agent_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "message_id": message_id,
                "role": role,
                "content": content,
                "workspace_id": workspace_id,
                "wing": wing,
                "room": room,
                "created_at": created_at or utc_now_iso(),
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": None,
                "last_error": None,
                "traceparent": traceparent,
                "lease_owner": None,
                "lease_expires_at": None,
                "palace_id": palace_id,
            }
        )
        return row_id

    async def claim_batch(
        self,
        *,
        agent_id: str,
        lease_owner: str,
        limit: int = 16,
        lease_seconds: int = 30,
        palace_id: str = "",
    ) -> list[Any]:
        now = datetime.now(UTC)
        now_iso = now.isoformat(timespec="seconds")
        expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        query = self._col.where(filter=FieldFilter("agent_id", "==", agent_id)).where(
            filter=FieldFilter("status", "==", "pending")
        )
        if palace_id:
            query = query.where(filter=FieldFilter("palace_id", "in", [palace_id, ""]))
        query = query.order_by("created_at").limit(limit * 4)
        docs = [doc async for doc in query.stream()]
        claimed: list[Any] = []
        for doc in docs:
            if len(claimed) >= limit:
                break
            transaction = self._client.transaction()

            async def _claim_body(
                txn: firestore.AsyncTransaction,
                ref: firestore.AsyncDocumentReference,
            ) -> Any | None:
                snapshot = await ref.get(transaction=txn)
                if not snapshot.exists:
                    return None
                data = snapshot.to_dict() or {}
                if str(data.get("status")) != "pending":
                    return None
                row_palace = str(data.get("palace_id") or "")
                if palace_id and row_palace and row_palace != palace_id:
                    return None
                next_at = data.get("next_attempt_at")
                if next_at and str(next_at) > now_iso:
                    return None
                txn.update(
                    ref,
                    {
                        "status": "processing",
                        "lease_owner": lease_owner,
                        "lease_expires_at": expires,
                        "attempts": int(data.get("attempts") or 0) + 1,
                        "palace_id": row_palace or palace_id,
                    },
                )
                return self._row_from_data(ref.id, data)

            claim_txn = cast(
                Callable[
                    [firestore.AsyncTransaction, firestore.AsyncDocumentReference],
                    Awaitable[Any | None],
                ],
                firestore.async_transactional(_claim_body),
            )
            row = await claim_txn(transaction, doc.reference)
            if row is not None:
                claimed.append(row)
        expired_q = (
            self._col.where(filter=FieldFilter("agent_id", "==", agent_id))
            .where(filter=FieldFilter("status", "==", "processing"))
            .where(filter=FieldFilter("lease_expires_at", "<", now_iso))
        )
        async for doc in expired_q.stream():
            transaction = self._client.transaction()

            async def _release_body(
                txn: firestore.AsyncTransaction,
                ref: firestore.AsyncDocumentReference,
            ) -> None:
                snapshot = await ref.get(transaction=txn)
                if not snapshot.exists:
                    return
                data = snapshot.to_dict() or {}
                if str(data.get("status")) != "processing":
                    return
                expires_at = data.get("lease_expires_at")
                if not expires_at or str(expires_at) >= now_iso:
                    return
                txn.update(
                    ref,
                    {"status": "pending", "lease_owner": None, "lease_expires_at": None},
                )

            release_txn = cast(
                Callable[
                    [firestore.AsyncTransaction, firestore.AsyncDocumentReference],
                    Awaitable[None],
                ],
                firestore.async_transactional(_release_body),
            )
            await release_txn(transaction, doc.reference)
        return claimed

    async def mark_committed(self, row_ids: list[str], *, lease_owner: str | None = None) -> int:
        n = 0
        for row_id in row_ids:
            transaction = self._client.transaction()

            async def _commit_body(
                txn: firestore.AsyncTransaction,
                ref: firestore.AsyncDocumentReference,
            ) -> bool:
                snapshot = await ref.get(transaction=txn)
                if not snapshot.exists:
                    return False
                data = snapshot.to_dict() or {}
                if lease_owner and str(data.get("lease_owner") or "") != lease_owner:
                    return False
                txn.update(
                    ref,
                    {
                        "status": "committed",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "last_error": None,
                        "next_attempt_at": None,
                    },
                )
                return True

            commit_txn = cast(
                Callable[
                    [firestore.AsyncTransaction, firestore.AsyncDocumentReference],
                    Awaitable[bool],
                ],
                firestore.async_transactional(_commit_body),
            )
            if await commit_txn(transaction, self._ref(row_id)):
                n += 1
        return n

    async def mark_retry(
        self,
        row_id: str,
        *,
        error_class: str,
        attempts: int,
        permanent: bool | None = None,
        lease_owner: str | None = None,
    ) -> int:
        dead = bool(permanent) if permanent is not None else is_permanent_error(error_class)
        status = STATUS_DEAD if dead else STATUS_PENDING
        next_at = None if status == STATUS_DEAD else backoff_iso(attempts)
        transaction = self._client.transaction()

        async def _retry_body(
            txn: firestore.AsyncTransaction,
            ref: firestore.AsyncDocumentReference,
        ) -> bool:
            snapshot = await ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if lease_owner and str(data.get("lease_owner") or "") != lease_owner:
                return False
            txn.update(
                ref,
                {
                    "status": status,
                    "last_error": error_class,
                    "next_attempt_at": next_at,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
            )
            return True

        retry_txn = cast(
            Callable[
                [firestore.AsyncTransaction, firestore.AsyncDocumentReference],
                Awaitable[bool],
            ],
            firestore.async_transactional(_retry_body),
        )
        return 1 if await retry_txn(transaction, self._ref(row_id)) else 0

    async def gc_committed(self, *, days: int = 7) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        query = self._col.where(filter=FieldFilter("status", "==", "committed"))
        n = 0
        async for doc in query.stream():
            data = doc.to_dict() or {}
            created = str(data.get("created_at") or "")
            if created and created < cutoff:
                await doc.reference.delete()
                n += 1
        return n

    async def pending_depth(self, *, agent_id: str | None = None) -> tuple[int, float]:
        query: Any = self._col.where(filter=FieldFilter("status", "in", ["pending", "processing"]))
        if agent_id:
            query = query.where(filter=FieldFilter("agent_id", "==", agent_id))
        count = 0
        oldest: str | None = None
        async for doc in query.stream():
            data = doc.to_dict() or {}
            count += 1
            created = str(data.get("created_at") or "")
            if created and (oldest is None or created < oldest):
                oldest = created
        age = 0.0
        if oldest:
            try:
                created_dt = datetime.fromisoformat(oldest)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=UTC)
                age = max(0.0, (datetime.now(UTC) - created_dt).total_seconds())
            except ValueError:
                age = 0.0
        return count, age

    async def dead_depth(self, *, agent_id: str | None = None) -> int:
        query: Any = self._col.where(filter=FieldFilter("status", "==", "dead"))
        if agent_id:
            query = query.where(filter=FieldFilter("agent_id", "==", agent_id))
        return len([doc async for doc in query.stream()])

    async def close(self) -> None:
        return


class FirestoreStorageBackend:
    """Firestore-backed storage backend using ``google.cloud.firestore.AsyncClient``."""

    shares_outbox = True

    def __init__(self, config: FirestoreConfig) -> None:
        self._config = config
        self._client: AsyncClient | None = None
        self._history_store: FirestoreHistoryStore | None = None
        self._usage_store: FirestoreUsageStore | None = None
        self._runs_store: FirestoreRunStore | None = None
        self._scheduled_loops_store: FirestoreScheduledLoopStore | None = None
        self._session_turn_lock_store: FirestoreSessionTurnLockStore | None = None
        self._outbox_store: FirestoreOutboxStore | None = None

    async def open(self, *, run_schema: bool = True) -> None:
        """Open the Firestore client.

        ``run_schema`` is accepted for API parity with other backends; Firestore
        is schemaless and performs no DDL.
        """
        self._client = AsyncClient(
            project=self._config.project,
            database=self._config.database,
        )
        prefix = self._config.prefix
        self._history_store = FirestoreHistoryStore(self._client, prefix)
        self._usage_store = FirestoreUsageStore(self._client, prefix)
        self._runs_store = FirestoreRunStore(self._client, prefix)
        self._scheduled_loops_store = FirestoreScheduledLoopStore(self._client, prefix)
        self._session_turn_lock_store = FirestoreSessionTurnLockStore(self._client, prefix)
        self._outbox_store = FirestoreOutboxStore(self._client, prefix)

    async def close(self) -> None:
        if self._client is not None:
            close_fn = cast(Callable[[], None], self._client.close)
            close_fn()
            self._client = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None
            self._scheduled_loops_store = None
            self._session_turn_lock_store = None
            self._outbox_store = None

    def history(self) -> FirestoreHistoryStore:
        if self._history_store is None:
            raise RuntimeError("FirestoreStorageBackend.open() has not been called")
        return self._history_store

    def usage(self) -> FirestoreUsageStore:
        if self._usage_store is None:
            raise RuntimeError("FirestoreStorageBackend.open() has not been called")
        return self._usage_store

    def runs(self) -> FirestoreRunStore:
        if self._runs_store is None:
            raise RuntimeError("FirestoreStorageBackend.open() has not been called")
        return self._runs_store

    def scheduled_loops(self) -> FirestoreScheduledLoopStore:
        if self._scheduled_loops_store is None:
            raise RuntimeError("FirestoreStorageBackend.open() has not been called")
        return self._scheduled_loops_store

    def session_turns(self) -> FirestoreSessionTurnLockStore:
        if self._session_turn_lock_store is None:
            raise RuntimeError("FirestoreStorageBackend.open() has not been called")
        return self._session_turn_lock_store

    def outbox(self) -> FirestoreOutboxStore:
        if self._outbox_store is None:
            raise RuntimeError("FirestoreStorageBackend.open() has not been called")
        return self._outbox_store
