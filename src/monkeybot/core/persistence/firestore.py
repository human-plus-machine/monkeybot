"""Firestore storage backend (requires ``pip install 'monkeybot[firestore]'``)."""

from __future__ import annotations

import json
import logging
import time
from typing import cast

from google.cloud import firestore
from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.llm.usage import Usage, UsageSummary
from monkeybot.core.persistence.backends import FirestoreConfig
from monkeybot.core.persistence.durable_runs import (
    SubagentEnvelope,
    SubagentRunRow,
    _tuple_to_run_row,
)
from monkeybot.core.persistence.thread_summary import ChatThreadSummary, preview_from_content_blob
from monkeybot.core.types.content_blocks import ContentBlock

logger = logging.getLogger(__name__)

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")


def _collection_name(prefix: str, base: str) -> str:
    if not prefix:
        return base
    return f"{prefix}_{base}"


class FirestoreHistoryStore:
    """Firestore-backed conversation history store."""

    def __init__(self, client: AsyncClient, prefix: str) -> None:
        self._client = client
        self._collection = _collection_name(prefix, "conversation_history")

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
            }
        )

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        query = (
            self._client.collection(self._collection)
            .where(filter=FieldFilter("thread_id", "==", thread_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        rows = [doc async for doc in query.stream()]
        rows_chrono = list(reversed(rows))
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
                    "Unparseable history row id=%s thread_id=%s",
                    row_id,
                    thread_id,
                    exc_info=True,
                )
                raise ValueError(f"history row {row_id} unparseable: {exc}") from exc
            if role not in _VALID_ROLES:
                raise ValueError(f"history row {row_id} has invalid role: {role!r}")
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
        for msg in messages:
            await self.append(thread_id, msg)

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        cap = max(1, min(limit, 200))
        query = (
            self._client.collection(self._collection)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(2000)
        )
        counts: dict[str, int] = {}
        latest: dict[str, tuple[int, str, str]] = {}
        async for doc in query.stream():
            data = doc.to_dict() or {}
            thread_id = str(data.get("thread_id", ""))
            if not thread_id:
                continue
            counts[thread_id] = counts.get(thread_id, 0) + 1
            created_at = int(data.get("created_at", 0))
            role = str(data.get("role", ""))
            content = str(data.get("content", ""))
            prev = latest.get(thread_id)
            if prev is None or created_at >= prev[0]:
                latest[thread_id] = (created_at, role, content)
        rows = sorted(latest.items(), key=lambda item: item[1][0], reverse=True)[:cap]
        out: list[ChatThreadSummary] = []
        for thread_id, (last_at, _role, content) in rows:
            preview = preview_from_content_blob(content)
            out.append(
                ChatThreadSummary(
                    thread_id=thread_id,
                    last_message_at=last_at,
                    message_count=counts.get(thread_id, 0),
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
        query = self._client.collection(self._collection)
        if thread_id is not None:
            query = query.where(filter=FieldFilter("thread_id", "==", thread_id))
        rows: list[dict[str, object]] = []
        async for doc in query.stream():
            data = doc.to_dict() or {}
            created_at = int(data.get("created_at", 0))
            if since_ms is not None and created_at < since_ms:
                continue
            rows.append(data)
        return rows

    async def summary(
        self,
        thread_id: str | None = None,
        since_ms: int | None = None,
    ) -> UsageSummary:
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

        input_tokens = sum(int(r.get("input_tokens", 0)) for r in rows)
        output_tokens = sum(int(r.get("output_tokens", 0)) for r in rows)
        cached_tokens = sum(int(r.get("cached_tokens", 0)) for r in rows)
        cost_usd = sum(float(r.get("cost_usd", 0.0)) for r in rows)
        cache_read_tokens = sum(int(r.get("cache_read_tokens", 0)) for r in rows)
        cache_creation_tokens = sum(int(r.get("cache_creation_tokens", 0)) for r in rows)
        created_times = [int(r.get("created_at", 0)) for r in rows]

        last_pt = 0
        last_est = 0
        if thread_id is not None:
            latest = max(rows, key=lambda r: int(r.get("created_at", 0)))
            last_pt = int(latest.get("input_tokens", 0))
            last_est = int(latest.get("estimated_prompt_tokens", 0))

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


def _doc_to_run_row(doc_id: str, data: dict[str, object]) -> SubagentRunRow:
    row = (
        doc_id,
        data.get("parent_run_id"),
        data.get("script"),
        data.get("envelope_json"),
        data.get("status"),
        data.get("result_json"),
        data.get("error_json"),
        data.get("started_at"),
        data.get("finished_at"),
        data.get("scratch_dir"),
        data.get("worker_id"),
        data.get("claimed_at"),
    )
    return _tuple_to_run_row(row)


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
        now_ms = int(time.time() * 1000)
        await self._doc(run_id).set(
            {
                "parent_run_id": parent_run_id,
                "script": script,
                "envelope_json": envelope.to_json(),
                "status": "pending",
                "result_json": None,
                "error_json": None,
                "started_at": now_ms,
                "finished_at": None,
                "scratch_dir": str(scratch_dir),
                "worker_id": None,
                "claimed_at": None,
            }
        )

    async def record_started(
        self,
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
                "status": "running",
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

        @firestore.async_transactional
        async def _claim_in_txn(txn: firestore.AsyncTransaction, ref: firestore.AsyncDocumentReference) -> bool:
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

        return await _claim_in_txn(transaction, doc_ref)

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

    async def record_completed(self, run_id: str, result_json: str) -> None:
        now_ms = int(time.time() * 1000)
        await self._doc(run_id).update(
            {
                "status": "completed",
                "result_json": result_json,
                "finished_at": now_ms,
                "error_json": None,
            }
        )

    async def record_failed(self, run_id: str, error: str) -> None:
        now_ms = int(time.time() * 1000)
        err_payload = json.dumps({"message": error})
        await self._doc(run_id).update(
            {
                "status": "failed",
                "error_json": err_payload,
                "finished_at": now_ms,
            }
        )

    async def pending_runs(self) -> list[SubagentRunRow]:
        rows: list[SubagentRunRow] = []
        for status in ("pending", "running"):
            query = (
                self._client.collection(self._collection)
                .where(filter=FieldFilter("status", "==", status))
                .order_by("started_at")
            )
            async for doc in query.stream():
                rows.append(_doc_to_run_row(doc.id, doc.to_dict() or {}))
        rows.sort(key=lambda r: r.started_at)
        return rows

    async def get_run(self, run_id: str) -> SubagentRunRow | None:
        snapshot = await self._doc(run_id).get()
        if not snapshot.exists:
            return None
        return _doc_to_run_row(snapshot.id, snapshot.to_dict() or {})


class FirestoreStorageBackend:
    """Firestore-backed storage backend using ``google.cloud.firestore.AsyncClient``."""

    def __init__(self, config: FirestoreConfig) -> None:
        self._config = config
        self._client: AsyncClient | None = None
        self._history_store: FirestoreHistoryStore | None = None
        self._usage_store: FirestoreUsageStore | None = None
        self._runs_store: FirestoreRunStore | None = None

    async def open(self) -> None:
        self._client = AsyncClient(
            project=self._config.project,
            database=self._config.database,
        )
        prefix = self._config.prefix
        self._history_store = FirestoreHistoryStore(self._client, prefix)
        self._usage_store = FirestoreUsageStore(self._client, prefix)
        self._runs_store = FirestoreRunStore(self._client, prefix)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None

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
