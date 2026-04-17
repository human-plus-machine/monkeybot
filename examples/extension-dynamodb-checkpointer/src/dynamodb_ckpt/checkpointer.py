"""DynamoDB Checkpointer — reference harness extension.

A single-table, composite-key implementation of the monkey-bot
:class:`Checkpointer` ABC. The whole backend is deliberately small (~100 LOC)
to demonstrate that the extensibility contract does not require framework
changes or helper packages — only the public ABC and a third-party SDK.

Table schema (created out-of-band by the operator — see README.md):

    ┌──────────────┬───────┬───────────────────────────────────────────┐
    │ Attribute    │ Type  │ Role                                      │
    ├──────────────┼───────┼───────────────────────────────────────────┤
    │ session_id   │ S     │ Partition key                             │
    │ checkpoint_id│ S     │ Sort key (monotonic id, ULID-shaped)      │
    │ payload      │ B     │ Serialized Mapping (pickle, UTF-8 bytes)  │
    │ reason       │ S     │ Enum: turn_end|pre_destructive|manual|    │
    │              │       │       rewind                               │
    │ created_at   │ S     │ ISO-8601 UTC timestamp                    │
    │ bytes        │ N     │ Payload size                              │
    │ uri          │ S     │ dynamodb://<table>/<session>/<checkpoint> │
    └──────────────┴───────┴───────────────────────────────────────────┘

Queries use ``ScanIndexForward=False`` on the primary key so ``list`` is
newest-first without a secondary index. ``delete_session`` paginates the
query result and issues ``BatchWriteItem`` deletes in 25-item chunks (the
DynamoDB API limit).
"""

from __future__ import annotations

import itertools
import pickle
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from emonk.core.harness.extensions import Checkpointer, CheckpointMissing, CheckpointRef


class DynamoDBCheckpointer(Checkpointer):
    """DynamoDB-backed Checkpointer using a single composite-key table.

    Attributes:
        table_name: The DynamoDB table to read/write.
        region: Optional AWS region override; defaults to the SDK resolution
            chain (env vars, ``~/.aws/config``, IAM role).
        endpoint_url: Optional endpoint URL for DynamoDB Local / moto testing.
    """

    def __init__(
        self,
        *,
        table_name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._endpoint_url = endpoint_url
        self._counters: dict[str, itertools.count[int]] = {}

    def _next_checkpoint_id(self, session_id: str) -> str:
        counter = self._counters.setdefault(session_id, itertools.count(1))
        seq = next(counter)
        return f"{seq:016d}-{uuid.uuid4().hex[:8]}"

    def _session(self) -> Any:
        # Lazy import keeps ``aioboto3`` off the hot path until first use and
        # lets the extension package install without forcing a heavy wheel
        # on consumers that never instantiate the class.
        import aioboto3

        return aioboto3.Session(region_name=self._region)

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        """Persist ``state`` under ``session_id`` and return a :class:`CheckpointRef`."""
        payload = pickle.dumps(dict(state))
        checkpoint_id = self._next_checkpoint_id(session_id)
        created_at = datetime.now(UTC)
        ref = CheckpointRef(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            reason=reason,
            created_at=created_at,
            bytes=len(payload),
            uri=f"dynamodb://{self._table_name}/{session_id}/{checkpoint_id}",
        )
        async with self._session().client("dynamodb", endpoint_url=self._endpoint_url) as ddb:
            await ddb.put_item(
                TableName=self._table_name,
                Item={
                    "session_id": {"S": session_id},
                    "checkpoint_id": {"S": checkpoint_id},
                    "payload": {"B": payload},
                    "reason": {"S": reason},
                    "created_at": {"S": created_at.isoformat()},
                    "bytes": {"N": str(len(payload))},
                    "uri": {"S": ref.uri},
                },
            )
        return ref

    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the stored payload for ``checkpoint_id`` or the latest write."""
        async with self._session().client("dynamodb", endpoint_url=self._endpoint_url) as ddb:
            if checkpoint_id is None:
                resp = await ddb.query(
                    TableName=self._table_name,
                    KeyConditionExpression="session_id = :sid",
                    ExpressionAttributeValues={":sid": {"S": session_id}},
                    Limit=1,
                    ScanIndexForward=False,
                )
                items = resp.get("Items", [])
                if not items:
                    return None
                return _deserialize(items[0]["payload"]["B"])
            resp = await ddb.get_item(
                TableName=self._table_name,
                Key={
                    "session_id": {"S": session_id},
                    "checkpoint_id": {"S": checkpoint_id},
                },
            )
            item = resp.get("Item")
            if not item:
                raise CheckpointMissing(session_id, checkpoint_id)
            return _deserialize(item["payload"]["B"])

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        """Return checkpoint refs for ``session_id`` newest-first up to ``limit``."""
        async with self._session().client("dynamodb", endpoint_url=self._endpoint_url) as ddb:
            resp = await ddb.query(
                TableName=self._table_name,
                KeyConditionExpression="session_id = :sid",
                ExpressionAttributeValues={":sid": {"S": session_id}},
                Limit=limit,
                ScanIndexForward=False,
            )
        return [_to_ref(item) for item in resp.get("Items", [])]

    async def delete_session(self, session_id: str) -> None:
        """Delete every checkpoint belonging to ``session_id``."""
        async with self._session().client("dynamodb", endpoint_url=self._endpoint_url) as ddb:
            keys: list[dict[str, Any]] = []
            last_evaluated: Mapping[str, Any] | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "TableName": self._table_name,
                    "KeyConditionExpression": "session_id = :sid",
                    "ExpressionAttributeValues": {":sid": {"S": session_id}},
                    "ProjectionExpression": "session_id, checkpoint_id",
                }
                if last_evaluated:
                    kwargs["ExclusiveStartKey"] = last_evaluated
                resp = await ddb.query(**kwargs)
                keys.extend(
                    {
                        "session_id": item["session_id"],
                        "checkpoint_id": item["checkpoint_id"],
                    }
                    for item in resp.get("Items", [])
                )
                last_evaluated = resp.get("LastEvaluatedKey")
                if not last_evaluated:
                    break
            for chunk_start in range(0, len(keys), 25):
                chunk = keys[chunk_start : chunk_start + 25]
                await ddb.batch_write_item(
                    RequestItems={
                        self._table_name: [{"DeleteRequest": {"Key": key}} for key in chunk]
                    }
                )
        self._counters.pop(session_id, None)


def _deserialize(payload: bytes) -> Mapping[str, Any]:
    result: Any = pickle.loads(payload)  # noqa: S301 - trusted boundary; see README.md security note
    if not isinstance(result, Mapping):
        raise TypeError(
            f"DynamoDBCheckpointer expected a Mapping payload, got {type(result).__name__}"
        )
    return result


def _to_ref(item: Mapping[str, Any]) -> CheckpointRef:
    return CheckpointRef(
        session_id=item["session_id"]["S"],
        checkpoint_id=item["checkpoint_id"]["S"],
        reason=item["reason"]["S"],
        created_at=datetime.fromisoformat(item["created_at"]["S"]),
        bytes=int(item["bytes"]["N"]),
        uri=item["uri"]["S"],
    )


__all__ = ["DynamoDBCheckpointer"]
