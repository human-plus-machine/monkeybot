"""Discriminated-union spec for the ``Checkpointer`` extension surface.

See 1b-contracts.md §4.2.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import CoreSchema


class _CheckpointerBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    import_path: str | None = None


class CheckpointerInMemorySpec(_CheckpointerBase):
    """Non-durable in-process checkpointer."""

    backend: Literal["in_memory"] = "in_memory"


class CheckpointerFirestoreSpec(_CheckpointerBase):
    """Cloud Firestore-backed checkpointer."""

    backend: Literal["firestore"] = "firestore"
    project_id: str | None = None
    collection: str = "checkpoints"


class CheckpointerPostgresSpec(_CheckpointerBase):
    """Postgres-backed checkpointer."""

    backend: Literal["postgres"] = "postgres"
    dsn_env: str = "CKPT_DSN"
    schema_name: str = "emonk_ckpt"
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 5_000


class CheckpointerMongoSpec(_CheckpointerBase):
    """MongoDB-backed checkpointer."""

    backend: Literal["mongo"] = "mongo"
    uri_env: str = "MONGO_URI"
    database: str = "emonk"
    collection: str = "checkpoints"
    require_replica_set: bool = False


_CHECKPOINTER_UNION = Annotated[
    CheckpointerInMemorySpec | CheckpointerFirestoreSpec | CheckpointerPostgresSpec | CheckpointerMongoSpec,
    Field(discriminator="backend"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(_CHECKPOINTER_UNION)


class CheckpointerSpec:
    """Discriminated-union wrapper exposing ``model_validate`` for consumers.

    Used both as a Pydantic field type (``checkpointer: CheckpointerSpec | None``)
    and as a classmethod-style validator
    (``CheckpointerSpec.model_validate({...})``).
    """

    @classmethod
    def model_validate(cls, data: Any) -> _CheckpointerBase:
        """Validate ``data`` into the correct concrete checkpointer sub-spec."""
        return _ADAPTER.validate_python(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
        return _ADAPTER.core_schema


__all__ = [
    "CheckpointerFirestoreSpec",
    "CheckpointerInMemorySpec",
    "CheckpointerMongoSpec",
    "CheckpointerPostgresSpec",
    "CheckpointerSpec",
]
