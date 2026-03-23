"""Firestore-backed LangGraph checkpointer for persistent conversation memory.

Persists LangGraph checkpoint state to Firestore so conversation history
survives Cloud Run container restarts, scale-to-zero, and new deployments.

Data model::

    {collection}/                    # top-level collection (default: agent_checkpoints)
      {sanitized_thread_id}/         # document per user thread
        checkpoints/                 # subcollection: one doc per graph turn
          {checkpoint_id}
            checkpoint_ns: str
            checkpoint_type: str     # serde type tag
            checkpoint_data: str     # base64-encoded serde bytes
            metadata: dict
            new_versions: dict
            parent_checkpoint_id: str | None
            created_at: Timestamp
        writes/                      # subcollection: pending task writes
          {checkpoint_id}_{task_id}_{idx}
            checkpoint_id: str
            task_id: str
            channel: str
            type: str
            data: str                # base64-encoded serde bytes
            created_at: Timestamp

Note: ``from __future__ import annotations`` is required because
BaseCheckpointSaver defines a method named ``list``, shadowing the builtin
inside the class body. Without deferred annotation evaluation,
``-> list[tuple[...]]`` raises TypeError at class definition time.
"""
from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from google.cloud.firestore_v1 import FieldFilter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
)

logger = logging.getLogger(__name__)


def _safe_thread_id(thread_id: str) -> str:
    """Sanitize a thread ID (email) for use as a Firestore document ID."""
    return thread_id.replace("@", "_at_").replace(".", "_dot_")


class FirestoreCheckpointSaver(BaseCheckpointSaver):
    """Async Firestore checkpointer for LangGraph agents on Cloud Run.

    Uses google.cloud.firestore.AsyncClient so all I/O is non-blocking.
    The Firestore client is lazy-initialized on first use and reused across
    requests on the same container instance.

    Usage:
        checkpointer = FirestoreCheckpointSaver(project_id="aurigaos")
        agent = build_deep_agent(..., checkpointer=checkpointer)
    """

    def __init__(
        self,
        project_id: str,
        collection: str = "agent_checkpoints",
    ) -> None:
        super().__init__()
        self._project_id = project_id
        self._collection_name = collection
        self._db = None
        logger.info(
            "FirestoreCheckpointSaver configured",
            extra={"project_id": project_id, "collection": collection},
        )

    @property
    def db(self):
        """Lazy-initialize the async Firestore client (same pattern as scheduler/storage.py)."""
        if self._db is None:
            from google.cloud import firestore  # noqa: PLC0415
            self._db = firestore.AsyncClient(project=self._project_id)
        return self._db

    def _thread_doc(self, thread_id: str):
        return self.db.collection(self._collection_name).document(_safe_thread_id(thread_id))

    def _checkpoints_col(self, thread_id: str):
        return self._thread_doc(thread_id).collection("checkpoints")

    def _writes_col(self, thread_id: str):
        return self._thread_doc(thread_id).collection("writes")

    # ------------------------------------------------------------------
    # Async methods — primary interface for Cloud Run async workloads
    # ------------------------------------------------------------------

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Serialize and store a checkpoint in Firestore."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str = checkpoint["id"]
        parent_id: str | None = config["configurable"].get("checkpoint_id")

        type_, data = self.serde.dumps_typed(checkpoint)

        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # noqa: PLC0415

        await self._checkpoints_col(thread_id).document(checkpoint_id).set({
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_type": type_,
            "checkpoint_data": base64.b64encode(data).decode(),
            # Firestore rejects field names with double-underscore prefix/suffix (e.g. __start__).
            # LangGraph's ChannelVersions uses such keys, so serialize as JSON string instead.
            "metadata": json.dumps(metadata),
            "new_versions": json.dumps(dict(new_versions)),
            "parent_checkpoint_id": parent_id,
            "created_at": SERVER_TIMESTAMP,
        })

        logger.debug("Stored checkpoint %s for thread %s", checkpoint_id, thread_id)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Fetch the latest (or specific) checkpoint for a thread."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str | None = config["configurable"].get("checkpoint_id")

        col = self._checkpoints_col(thread_id)

        if checkpoint_id:
            doc = await col.document(checkpoint_id).get()
            if not doc.exists:
                return None
            doc_data = doc.to_dict()
            resolved_id = checkpoint_id
        else:
            from google.cloud.firestore_v1.base_query import BaseQuery 
            query = (
                col.where(filter=FieldFilter("checkpoint_ns", "==", checkpoint_ns))
                .order_by("created_at", direction=BaseQuery.DESCENDING)
                .limit(1)
            )
            docs = [d async for d in query.stream()]
            if not docs:
                return None
            doc = docs[0]
            doc_data = doc.to_dict()
            resolved_id = doc.id

        checkpoint = self.serde.loads_typed((
            doc_data["checkpoint_type"],
            base64.b64decode(doc_data["checkpoint_data"]),
        ))

        pending_writes = await self._load_pending_writes(thread_id, resolved_id)

        parent_checkpoint_id = doc_data.get("parent_checkpoint_id")
        parent_config: RunnableConfig | None = None
        if parent_checkpoint_id:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_checkpoint_id,
                }
            }

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": resolved_id,
                }
            },
            checkpoint=checkpoint,
            metadata=json.loads(doc_data["metadata"]) if isinstance(doc_data["metadata"], str) else doc_data["metadata"],
            parent_config=parent_config,
            pending_writes=pending_writes or None,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Yield checkpoints for a thread in reverse chronological order."""
        if config is None:
            return

        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")

        from google.cloud.firestore_v1.base_query import BaseQuery  # noqa: PLC0415

        col = self._checkpoints_col(thread_id)
        query = (
            col.where(filter=FieldFilter("checkpoint_ns", "==", checkpoint_ns))
            .order_by("created_at", direction=BaseQuery.DESCENDING)
        )
        if limit:
            query = query.limit(limit)

        async for doc in query.stream():
            data = doc.to_dict()
            checkpoint = self.serde.loads_typed((
                data["checkpoint_type"],
                base64.b64decode(data["checkpoint_data"]),
            ))
            parent_checkpoint_id = data.get("parent_checkpoint_id")
            parent_config: RunnableConfig | None = None
            if parent_checkpoint_id:
                parent_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
            yield CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": doc.id,
                    }
                },
                checkpoint=checkpoint,
                metadata=json.loads(data["metadata"]) if isinstance(data["metadata"], str) else data["metadata"],
                parent_config=parent_config,
            )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending task writes for a checkpoint."""
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_id: str = config["configurable"].get("checkpoint_id", "")

        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # noqa: PLC0415

        writes_col = self._writes_col(thread_id)
        for idx, (channel, value) in enumerate(writes):
            type_, data = self.serde.dumps_typed(value)
            doc_id = f"{checkpoint_id}_{task_id}_{idx}"
            await writes_col.document(doc_id).set({
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
                "channel": channel,
                "type": type_,
                "data": base64.b64encode(data).decode(),
                "created_at": SERVER_TIMESTAMP,
            })

    # ------------------------------------------------------------------
    # Sync stubs — not used in async Cloud Run context
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("FirestoreCheckpointSaver is async-only; use aget_tuple.")

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError("FirestoreCheckpointSaver is async-only; use alist.")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError("FirestoreCheckpointSaver is async-only; use aput.")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("FirestoreCheckpointSaver is async-only; use aput_writes.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_pending_writes(
        self, thread_id: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any]]:
        """Fetch all pending writes for a checkpoint."""
        writes_col = self._writes_col(thread_id)
        query = writes_col.where(filter=FieldFilter("checkpoint_id", "==", checkpoint_id))
        pending: list[tuple[str, str, Any]] = []
        async for doc in query.stream():
            wd = doc.to_dict()
            value = self.serde.loads_typed((wd["type"], base64.b64decode(wd["data"])))
            pending.append((wd["task_id"], wd["channel"], value))
        return pending
