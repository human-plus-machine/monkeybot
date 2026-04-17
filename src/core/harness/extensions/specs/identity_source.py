"""Discriminated-union spec for the ``IdentitySource`` extension surface.

See 1b-contracts.md §4.2.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import CoreSchema


class _IdentitySourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    import_path: str | None = None
    cache_ttl_seconds: int = 300
    allow_cross_principal: bool = False


class IdentitySourceLocalFSSpec(_IdentitySourceBase):
    """Local filesystem identity source."""

    backend: Literal["local_fs"] = "local_fs"
    dir: str = "./data/memory"
    per_principal_subdir: bool = True


class IdentitySourceS3Spec(_IdentitySourceBase):
    """AWS S3-backed identity source."""

    backend: Literal["s3"] = "s3"
    bucket: str
    prefix: str = "identity/"
    region: str | None = None
    sse: Literal["AES256", "aws:kms", None] = "AES256"
    kms_key_id: str | None = None


class IdentitySourceGCSSpec(_IdentitySourceBase):
    """Google Cloud Storage-backed identity source."""

    backend: Literal["gcs"] = "gcs"
    bucket: str
    prefix: str = "identity/"


class IdentitySourcePostgresSpec(_IdentitySourceBase):
    """Postgres-backed identity source."""

    backend: Literal["postgres"] = "postgres"
    dsn_env: str = "CKPT_DSN"
    schema_name: str = "emonk_identity"


class IdentitySourceMongoSpec(_IdentitySourceBase):
    """MongoDB-backed identity source."""

    backend: Literal["mongo"] = "mongo"
    uri_env: str = "MONGO_URI"
    database: str = "emonk"
    collection: str = "identity"


class IdentitySourceCallableSpec(_IdentitySourceBase):
    """Runtime-only identity source backed by a callable.

    ``import_path`` is required and must resolve to an awaitable that accepts
    ``(principal, session_id)``.
    """

    backend: Literal["callable"] = "callable"
    import_path: str | None = None


_IDENTITY_SOURCE_UNION = Annotated[
    IdentitySourceLocalFSSpec | IdentitySourceS3Spec | IdentitySourceGCSSpec | IdentitySourcePostgresSpec | IdentitySourceMongoSpec | IdentitySourceCallableSpec,
    Field(discriminator="backend"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(_IDENTITY_SOURCE_UNION)


class IdentitySourceSpec:
    """Discriminated-union wrapper for the identity-source surface."""

    @classmethod
    def model_validate(cls, data: Any) -> _IdentitySourceBase:
        return _ADAPTER.validate_python(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
        return _ADAPTER.core_schema


__all__ = [
    "IdentitySourceCallableSpec",
    "IdentitySourceGCSSpec",
    "IdentitySourceLocalFSSpec",
    "IdentitySourceMongoSpec",
    "IdentitySourcePostgresSpec",
    "IdentitySourceS3Spec",
    "IdentitySourceSpec",
]
