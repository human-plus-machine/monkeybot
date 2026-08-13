"""Firestore storage backend (requires ``pip install 'monkeybot[firestore]'``)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import cast

from google.cloud import firestore
from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.llm.usage import Usage, UsageBreakdown, UsageBucket, UsageSummary
from monkeybot.core.persistence.backends import FirestoreConfig
from monkeybot.core.persistence.firestore_scheduled_loops import FirestoreScheduledLoopStore
from monkeybot.core.persistence.durable_runs import (
    SubagentEnvelope,
    SubagentRunRow,
)
from monkeybot.core.persistence.thread_summary import ChatThreadSummary, preview_from_content_blob
from monkeybot.core.types.content_blocks import ContentBlock

logger = logging.getLogger(__name__)

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")


def _collection_name(prefix: str, base: str) -> str:
    if not prefix:
        return base
    return f"{prefix}_{base}"


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


_warned_legacy_unscoped_history = False


def _warn_legacy_unscoped_history_possible() -> None:
    """Log once per process that pre-migration Firestore documents may be unreachable.

    Unlike Postgres (a real ``agent_scope = ''`` column, queryable), Firestore
    documents written before this scoping existed have no ``agent_scope`` field
    at all — a query can't distinguish "field equals ''" from "field absent,"
    so ``FirestoreHistoryStore.load``/``reset``'s legacy-scope fallback (see
    ``_read_scopes``) only reaches docs written with ``agent_scope=''``
    (e.g. by an unscoped store), not truly pre-scoping documents. There is no
    cheap existence check for "field missing" in Firestore, so this warns
    unconditionally rather than only when legacy data is actually present.
    """
    global _warned_legacy_unscoped_history
    if _warned_legacy_unscoped_history:
        return
    _warned_legacy_unscoped_history = True
    logger.warning(
        "Firestore conversation_history/threads documents written before "
        "agent_scope existed have no agent_scope field and are not reachable "
        "by this (or any) agent-scoped store's load/list_threads/reset — "
        "Firestore cannot query for a missing field. If this deployment "
        "predates agent-scoped history, those documents need an operator-run "
        "backfill to set agent_scope before they become readable again."
    )


class FirestoreHistoryStore:
    """Firestore-backed conversation history store, scoped to ``agent_scope``.

    ``agent_scope`` isolates threads when one Firestore database is shared
    across gateways for different agent roots — without it, ``list_threads``
    would surface another agent's newest transcript. Defaults to ``''``
    (unscoped) for in-process/test callers; production gateways pass the
    resolved agent root.

    The compound ``agent_scope`` equality + ``last_message_at``/``created_at``
    order-by queries below require a Firestore composite index per collection;
    Firestore raises a clear error with a console link to create it on first use.
    """

    def __init__(self, client: AsyncClient, prefix: str, agent_scope: str = "") -> None:
        self._client = client
        self._collection = _collection_name(prefix, "conversation_history")
        self._threads_collection = _collection_name(prefix, "threads")
        self._agent_scope = agent_scope

    def _read_scopes(self) -> list[str]:
        """Scopes a read-by-known-``thread_id`` call should see.

        Includes the pre-migration ``''`` scope alongside the real one, same
        rationale as :meth:`PostgresHistoryStore._read_scopes`: an already-known
        ``thread_id`` (explicit ``--session``, transcript backfill) should still
        resolve after upgrading. ``list_threads`` does NOT use this — it would
        reopen the cross-agent leak this scoping exists to close.
        """
        return [self._agent_scope] if not self._agent_scope else [self._agent_scope, ""]

    def _summary_doc_id(self, thread_id: str, scope: str | None = None) -> str:
        """Scope-qualified key for the per-thread summary doc.

        Keying purely by ``thread_id`` let two agents that reuse the same
        explicit session id (nothing prevents ``--session <id>`` reuse across
        agents) share one summary document: each ``set(..., merge=True)``
        overwrote the other's ``agent_scope``/``message_count`` fields,
        flipping which agent's ``list_threads`` the thread appeared under and
        corrupting the message count. A short hash of the scope keeps summary
        docs disjoint per agent without leaking a raw (possibly path-shaped)
        scope value into a Firestore document id, which cannot contain ``/``.

        ``scope`` defaults to this store's own scope; pass the legacy ``''``
        scope explicitly to address the pre-migration summary doc (see reset()).
        """
        scope_key = hashlib.sha256((self._agent_scope if scope is None else scope).encode())
        return f"{scope_key.hexdigest()[:16]}:{thread_id}"

    async def _upsert_thread_summary(
        self,
        thread_id: str,
        *,
        created_at: int,
        content: str,
    ) -> None:
        """Maintain one summary doc per (scope, thread) for indexed ``list_threads`` reads."""
        thread_ref = self._client.collection(self._threads_collection).document(
            self._summary_doc_id(thread_id)
        )
        await thread_ref.set(
            {
                "thread_id": thread_id,
                "last_message_at": created_at,
                "message_count": firestore.Increment(1),
                "last_content": content,
                "agent_scope": self._agent_scope,
            },
            merge=True,
        )

    async def _delete_thread_summary(self, thread_id: str, *, scope: str | None = None) -> None:
        await self._client.collection(self._threads_collection).document(
            self._summary_doc_id(thread_id, scope)
        ).delete()

    async def append(self, thread_id: str, message: Message) -> None:
        role = message.role
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        await self._client.collection(self._collection).add(
            {
                "thread_id": thread_id,
                "role": role,
                "content": payload,
                "created_at": created_at,
                "agent_scope": self._agent_scope,
            }
        )
        await self._upsert_thread_summary(thread_id, created_at=created_at, content=payload)

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        scopes = self._read_scopes()
        if limit is not None:
            # Newest ``limit`` rows: query descending then reverse to chronological.
            query = (
                self._client.collection(self._collection)
                .where(filter=FieldFilter("thread_id", "==", thread_id))
                .where(filter=FieldFilter("agent_scope", "in", scopes))
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            rows = [doc async for doc in query.stream()]
            rows_chrono = list(reversed(rows))
        else:
            query = (
                self._client.collection(self._collection)
                .where(filter=FieldFilter("thread_id", "==", thread_id))
                .where(filter=FieldFilter("agent_scope", "in", scopes))
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
        """Replace thread history, deleting both this scope and the legacy ``''``
        scope for ``thread_id`` — an explicit reset of an already-known id fully
        adopts it into the current scope, same as :meth:`load`.
        """
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("thread_id", "==", thread_id))
            .where(filter=FieldFilter("agent_scope", "in", self._read_scopes()))
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
        for scope in self._read_scopes():
            await self._delete_thread_summary(thread_id, scope=scope)
        for msg in messages:
            await self.append(thread_id, msg)

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        cap = max(1, min(limit, 200))
        query = (
            self._client.collection(self._threads_collection)
            .where(filter=FieldFilter("agent_scope", "==", self._agent_scope))
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
            doc_stream = collection.where(
                filter=FieldFilter("thread_id", "==", thread_id)
            ).stream()
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]],
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]],
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]],
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]],
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]],
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
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("claimed_at_ms", "<", cutoff))
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[bool]],
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
            Callable[[firestore.AsyncTransaction, firestore.AsyncDocumentReference], Awaitable[None]],
            firestore.async_transactional(_release_body),
        )
        await release_txn(transaction, doc_ref)

    async def is_busy(self, session_id: str) -> bool:
        snapshot = await self._doc(session_id).get()
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        return data.get("request_id") is not None


class FirestoreStorageBackend:
    """Firestore-backed storage backend using ``google.cloud.firestore.AsyncClient``."""

    def __init__(self, config: FirestoreConfig, agent_scope: str = "") -> None:
        self._config = config
        self._agent_scope = agent_scope
        self._client: AsyncClient | None = None
        self._history_store: FirestoreHistoryStore | None = None
        self._usage_store: FirestoreUsageStore | None = None
        self._runs_store: FirestoreRunStore | None = None
        self._scheduled_loops_store: FirestoreScheduledLoopStore | None = None
        self._session_turn_lock_store: FirestoreSessionTurnLockStore | None = None

    async def open(self, *, run_schema: bool = True) -> None:
        """Open the Firestore client.

        ``run_schema`` is accepted for API parity with other backends; Firestore
        is schemaless and performs no DDL.
        """
        self._client = AsyncClient(
            project=self._config.project,
            database=self._config.database,
        )
        if self._agent_scope:
            _warn_legacy_unscoped_history_possible()
        prefix = self._config.prefix
        self._history_store = FirestoreHistoryStore(self._client, prefix, self._agent_scope)
        self._usage_store = FirestoreUsageStore(self._client, prefix)
        self._runs_store = FirestoreRunStore(self._client, prefix)
        self._scheduled_loops_store = FirestoreScheduledLoopStore(self._client, prefix)
        self._session_turn_lock_store = FirestoreSessionTurnLockStore(self._client, prefix)

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
