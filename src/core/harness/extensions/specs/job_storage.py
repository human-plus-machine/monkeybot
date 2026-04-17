"""Discriminated-union spec for the ``JobStorage`` extension surface.

See 1b-contracts.md §4.2.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import CoreSchema


class _JobStorageBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    import_path: str | None = None


class JobStorageJSONSpec(_JobStorageBase):
    """Single-process JSON file job storage."""

    backend: Literal["json"] = "json"
    path: str = "./data/jobs.json"


class JobStorageFirestoreSpec(_JobStorageBase):
    """Cloud Firestore-backed job storage."""

    backend: Literal["firestore"] = "firestore"
    project_id: str | None = None
    collection: str = "jobs"


class JobStoragePostgresSpec(_JobStorageBase):
    """Postgres-backed job storage."""

    backend: Literal["postgres"] = "postgres"
    dsn_env: str = "CKPT_DSN"
    schema_name: str = "emonk_scheduler"


class JobStorageMongoSpec(_JobStorageBase):
    """MongoDB-backed job storage."""

    backend: Literal["mongo"] = "mongo"
    uri_env: str = "MONGO_URI"
    database: str = "emonk"
    collection: str = "jobs"


_JOB_STORAGE_UNION = Annotated[
    JobStorageJSONSpec | JobStorageFirestoreSpec | JobStoragePostgresSpec | JobStorageMongoSpec,
    Field(discriminator="backend"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(_JOB_STORAGE_UNION)


class JobStorageSpec:
    """Discriminated-union wrapper for the job-storage surface."""

    @classmethod
    def model_validate(cls, data: Any) -> _JobStorageBase:
        return _ADAPTER.validate_python(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
        return _ADAPTER.core_schema


__all__ = [
    "JobStorageFirestoreSpec",
    "JobStorageJSONSpec",
    "JobStorageMongoSpec",
    "JobStoragePostgresSpec",
    "JobStorageSpec",
]
