"""MongoDB-backed :class:`IdentitySource` (Story 5).

Document shape::

    { _id: { principal_id: "...", file_name: "SOUL.md" }, content: "..." }

The collection uses a compound ``_id`` so writes are idempotent without
separate indexes. Motor is imported lazily inside
:mod:`_mongo_client`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .._mongo_client import get_client
from ..base import IdentitySource
from ..errors import IdentityNotFound
from ..values import LoadedIdentity, MemoryPatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...events import Principal


_FILE_TO_ATTR: dict[str, str] = {
    "SOUL.md": "soul",
    "RULES.md": "rules",
    "IDENTITY.md": "identity",
    "USER.md": "user",
    "INDEX.md": "index",
    "MEMORY.md": "memory",
    "HEARTBEAT.md": "heartbeat",
}


class MongoIdentitySource(IdentitySource):
    """Identity source persisted in a single Mongo collection.

    Args:
        uri_env: Env var name holding the Mongo URI.
        database: Target database (default ``"emonk"``).
        collection: Target collection (default ``"identity"``).
        cache_ttl_seconds: TTL advertised on the returned identity.
    """

    def __init__(
        self,
        *,
        uri_env: str = "MONGO_URI",
        database: str = "emonk",
        collection: str = "identity",
        cache_ttl_seconds: int = 300,
    ) -> None:
        self.uri_env = uri_env
        self.database = database
        self.collection_name = collection
        self.cache_ttl_seconds = cache_ttl_seconds
        self._collection: Any = None

    async def _ensure_collection(self) -> Any:
        if self._collection is None:
            client = await get_client(uri_env=self.uri_env)
            self._collection = client[self.database][self.collection_name]
        return self._collection

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Return the identity documents for ``principal`` as a :class:`LoadedIdentity`."""
        collection = await self._ensure_collection()
        cursor = collection.find({"_id.principal_id": principal.id})
        docs = await cursor.to_list(length=None)
        if not docs:
            raise IdentityNotFound(principal.id)

        values: dict[str, str] = dict.fromkeys(_FILE_TO_ATTR.values(), "")
        extras: dict[str, str] = {}
        seen: set[str] = set()
        for doc in docs:
            identifier = doc.get("_id") or {}
            file_name = str(identifier.get("file_name", ""))
            content = str(doc.get("content") or "")
            attr = _FILE_TO_ATTR.get(file_name)
            if attr is None:
                extras[f"extra_{file_name}"] = content
                continue
            values[attr] = content
            seen.add(file_name)
        for file_name in _FILE_TO_ATTR:
            if file_name not in seen:
                extras[f"missing_{file_name}"] = "1"

        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul=values["soul"],
            rules=values["rules"],
            identity=values["identity"],
            user=values["user"],
            index=values["index"],
            memory=values["memory"],
            heartbeat=values["heartbeat"],
            loaded_at=datetime.now(UTC),
            ttl_seconds=self.cache_ttl_seconds,
            source_backend="mongo",
            extras=extras,
        )

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        """Upsert or delete the principal's MEMORY.md / HEARTBEAT.md document."""
        collection = await self._ensure_collection()
        doc_id = {"principal_id": principal.id, "file_name": patch.target}
        if patch.operation == "delete":
            await collection.delete_one({"_id": doc_id})
            return
        if patch.operation == "append":
            current = await collection.find_one({"_id": doc_id})
            existing = str(current.get("content") or "") if current else ""
            new_content = existing + (patch.content or "")
        else:
            new_content = patch.content or ""
        await collection.update_one(
            {"_id": doc_id},
            {"$set": {"content": new_content, "updated_at": datetime.now(UTC)}},
            upsert=True,
        )


__all__ = ["MongoIdentitySource"]
