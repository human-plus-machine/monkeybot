"""S3-backed :class:`IdentitySource` (Story 5).

Objects live at ``s3://<bucket>/<prefix><principal_id>/<file>``. Missing
files inside a known principal folder are treated as empty strings (the
``extras`` dict records which files were missing so telemetry can detect
incomplete uploads). A completely missing principal folder raises
:class:`IdentityNotFound`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .._aws_clients import s3_client
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


class S3IdentitySource(IdentitySource):
    """Load identity bundles from S3. ``aioboto3`` is imported lazily.

    Args:
        bucket: S3 bucket name (required).
        prefix: Optional key prefix (trailing slash added automatically).
        region: Optional AWS region. ``None`` defers to SDK default.
        cache_ttl_seconds: Advertised TTL on the returned identity.
        sse / kms_key_id: Optional SSE settings applied to ``write_memory``.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        cache_ttl_seconds: int = 300,
        sse: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3IdentitySource requires a non-empty bucket name")
        self.bucket = bucket
        prefix = prefix.strip("/")
        self.prefix = f"{prefix}/" if prefix else ""
        self.region = region
        self.cache_ttl_seconds = cache_ttl_seconds
        self.sse = sse
        self.kms_key_id = kms_key_id

    def _object_key(self, principal_id: str, file_name: str) -> str:
        return f"{self.prefix}{principal_id}/{file_name}"

    def _put_kwargs(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        return extra

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Fetch every identity file for ``principal`` in parallel."""
        values: dict[str, str] = {}
        extras: dict[str, str] = {}
        any_found = False

        async with s3_client(self.region) as client:
            for attr, file_name in _FILE_FIELDS:
                key = self._object_key(principal.id, file_name)
                try:
                    response = await client.get_object(Bucket=self.bucket, Key=key)
                except Exception as exc:  # noqa: BLE001 - aioboto3 error shape varies
                    if _is_not_found(exc):
                        values[attr] = ""
                        extras[f"missing_{file_name}"] = "1"
                        continue
                    raise
                body = await response["Body"].read()
                values[attr] = body.decode("utf-8") if isinstance(body, bytes | bytearray) else str(body)
                any_found = True

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
            source_backend="s3",
            extras=extras,
        )

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        """Atomically rewrite the principal's MEMORY.md / HEARTBEAT.md object."""
        key = self._object_key(principal.id, patch.target)
        async with s3_client(self.region) as client:
            if patch.operation == "delete":
                await client.delete_object(Bucket=self.bucket, Key=key)
                return
            existing = ""
            if patch.operation == "append":
                try:
                    response = await client.get_object(Bucket=self.bucket, Key=key)
                    body = await response["Body"].read()
                    existing = body.decode("utf-8") if isinstance(body, bytes | bytearray) else str(body)
                except Exception as exc:  # noqa: BLE001
                    if not _is_not_found(exc):
                        raise
            payload = (existing + (patch.content or "")) if patch.operation == "append" else (patch.content or "")
            await client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload.encode("utf-8"),
                ContentType="text/markdown",
                **self._put_kwargs(),
            )


def _is_not_found(exc: Exception) -> bool:
    """Return ``True`` when ``exc`` signals a missing S3 key."""
    if hasattr(exc, "response"):
        status = (
            getattr(exc, "response", {})
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
        )
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if status == 404 or code in {"NoSuchKey", "404", "NotFound"}:
            return True
    name = type(exc).__name__
    return name in {"NoSuchKey", "404", "NotFound", "ClientError"} and (
        "404" in str(exc) or "NoSuchKey" in str(exc)
    )


__all__ = ["S3IdentitySource"]
