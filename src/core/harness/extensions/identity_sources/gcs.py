"""GCS-backed :class:`IdentitySource` (Story 5).

Objects live at ``gs://<bucket>/<prefix><principal_id>/<file>``. The
``google.cloud.storage`` client is imported lazily; blob I/O is wrapped in
:func:`asyncio.to_thread` so the middleware can stay fully async.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..base import IdentitySource
from ..errors import IdentityNotFound
from ..values import LoadedIdentity, MemoryPatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...events import Principal


_FILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("soul", "SOUL.md"),
    ("rules", "RULES.md"),
    ("identity", "IDENTITY.md"),
    ("user", "USER.md"),
    ("index", "INDEX.md"),
    ("memory", "MEMORY.md"),
    ("heartbeat", "HEARTBEAT.md"),
)


class GCSIdentitySource(IdentitySource):
    """Google Cloud Storage-backed identity source.

    Args:
        bucket: GCS bucket name (required).
        prefix: Optional blob prefix (trailing ``/`` added automatically).
        project_id: Optional GCP project id (``None`` uses ADC).
        cache_ttl_seconds: Advertised TTL on the returned identity.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        project_id: str | None = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        if not bucket:
            raise ValueError("GCSIdentitySource requires a non-empty bucket name")
        self.bucket_name = bucket
        prefix = prefix.strip("/")
        self.prefix = f"{prefix}/" if prefix else ""
        self.project_id = project_id
        self.cache_ttl_seconds = cache_ttl_seconds
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import storage  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise RuntimeError(
                    "GCSIdentitySource requires emonk[identity-source-gcs]"
                ) from exc
            self._client = storage.Client(project=self.project_id)
        return self._client

    def _bucket(self) -> Any:
        return self._get_client().bucket(self.bucket_name)

    def _blob_name(self, principal_id: str, file_name: str) -> str:
        return f"{self.prefix}{principal_id}/{file_name}"

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Fetch every identity blob for ``principal`` via a worker thread."""

        def _fetch() -> tuple[dict[str, str], dict[str, str], bool]:
            bucket = self._bucket()
            values: dict[str, str] = {}
            extras: dict[str, str] = {}
            any_found = False
            for attr, file_name in _FILE_FIELDS:
                blob = bucket.blob(self._blob_name(principal.id, file_name))
                if not blob.exists():
                    values[attr] = ""
                    extras[f"missing_{file_name}"] = "1"
                    continue
                values[attr] = blob.download_as_text()
                any_found = True
            return values, extras, any_found

        values, extras, any_found = await asyncio.to_thread(_fetch)
        if not any_found:
            raise IdentityNotFound(principal.id)

        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul=values.get("soul", ""),
            rules=values.get("rules", ""),
            identity=values.get("identity", ""),
            user=values.get("user", ""),
            index=values.get("index", ""),
            memory=values.get("memory", ""),
            heartbeat=values.get("heartbeat", ""),
            loaded_at=datetime.now(UTC),
            ttl_seconds=self.cache_ttl_seconds,
            source_backend="gcs",
            extras=extras,
        )

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        """Rewrite the principal's MEMORY.md / HEARTBEAT.md blob."""

        def _apply() -> None:
            blob = self._bucket().blob(self._blob_name(principal.id, patch.target))
            if patch.operation == "delete":
                if blob.exists():
                    blob.delete()
                return
            existing = blob.download_as_text() if blob.exists() else ""
            payload = (
                existing + (patch.content or "")
                if patch.operation == "append"
                else (patch.content or "")
            )
            blob.upload_from_string(payload, content_type="text/markdown")

        await asyncio.to_thread(_apply)


__all__ = ["GCSIdentitySource"]
