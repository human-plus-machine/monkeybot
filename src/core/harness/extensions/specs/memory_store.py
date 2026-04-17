"""Discriminated-union spec for the ``MemoryStore`` extension surface.

See 1b-contracts.md §4.2.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import CoreSchema


class _MemoryStoreBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    import_path: str | None = None
    require_vector_search: bool = False


class MemoryStoreInMemorySpec(_MemoryStoreBase):
    """Non-durable in-process memory store."""

    backend: Literal["in_memory"] = "in_memory"


class MemoryStoreFirestoreSpec(_MemoryStoreBase):
    """Cloud Firestore-backed memory store."""

    backend: Literal["firestore"] = "firestore"
    project_id: str | None = None
    collection: str = "memory"


class MemoryStoreGCSSpec(_MemoryStoreBase):
    """Google Cloud Storage-backed memory store."""

    backend: Literal["gcs"] = "gcs"
    bucket: str
    prefix: str = ""


class MemoryStoreS3Spec(_MemoryStoreBase):
    """AWS S3-backed memory store."""

    backend: Literal["s3"] = "s3"
    bucket: str
    prefix: str = ""
    region: str | None = None
    sse: Literal["AES256", "aws:kms", None] = None
    kms_key_id: str | None = None


class MemoryStorePostgresSpec(_MemoryStoreBase):
    """Postgres-backed memory store (pgvector optional)."""

    backend: Literal["postgres"] = "postgres"
    dsn_env: str = "CKPT_DSN"
    schema_name: str = "emonk_memory"
    enable_pgvector: bool = False


class MemoryStoreMongoSpec(_MemoryStoreBase):
    """MongoDB-backed memory store."""

    backend: Literal["mongo"] = "mongo"
    uri_env: str = "MONGO_URI"
    database: str = "emonk"
    collection: str = "memory"


_MEMORY_STORE_UNION = Annotated[
    MemoryStoreInMemorySpec | MemoryStoreFirestoreSpec | MemoryStoreGCSSpec | MemoryStoreS3Spec | MemoryStorePostgresSpec | MemoryStoreMongoSpec,
    Field(discriminator="backend"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(_MEMORY_STORE_UNION)


class MemoryStoreSpec:
    """Discriminated-union wrapper for the memory-store surface."""

    @classmethod
    def model_validate(cls, data: Any) -> _MemoryStoreBase:
        return _ADAPTER.validate_python(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
        return _ADAPTER.core_schema


__all__ = [
    "MemoryStoreFirestoreSpec",
    "MemoryStoreGCSSpec",
    "MemoryStoreInMemorySpec",
    "MemoryStoreMongoSpec",
    "MemoryStorePostgresSpec",
    "MemoryStoreS3Spec",
    "MemoryStoreSpec",
]
