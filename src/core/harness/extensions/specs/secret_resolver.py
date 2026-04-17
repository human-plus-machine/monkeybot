"""Discriminated-union spec for the ``SecretResolver`` extension surface.

See 1b-contracts.md §4.2.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import CoreSchema


class _SecretResolverBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    import_path: str | None = None


class SecretResolverEnvSpec(_SecretResolverBase):
    """Environment variable-backed secret resolver."""

    backend: Literal["env"] = "env"
    prefix: str = ""


class SecretResolverAWSSpec(_SecretResolverBase):
    """AWS Secrets Manager-backed secret resolver."""

    backend: Literal["aws_secrets_manager"] = "aws_secrets_manager"
    region: str | None = None
    cache_ttl_seconds: int = 60


class SecretResolverGCPSpec(_SecretResolverBase):
    """GCP Secret Manager-backed secret resolver."""

    backend: Literal["gcp_secret_manager"] = "gcp_secret_manager"
    project_id: str | None = None
    cache_ttl_seconds: int = 60


class SecretResolverCompositeSpec(_SecretResolverBase):
    """Composite secret resolver chaining multiple backends."""

    backend: Literal["composite"] = "composite"
    chain: list[str] = Field(default_factory=list)


_SECRET_RESOLVER_UNION = Annotated[
    SecretResolverEnvSpec | SecretResolverAWSSpec | SecretResolverGCPSpec | SecretResolverCompositeSpec,
    Field(discriminator="backend"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(_SECRET_RESOLVER_UNION)


def _default_secret_resolver_spec() -> SecretResolverEnvSpec:
    """Return the default secret-resolver spec when none is configured."""
    return SecretResolverEnvSpec()


class SecretResolverSpec:
    """Discriminated-union wrapper for the secret-resolver surface."""

    @classmethod
    def model_validate(cls, data: Any) -> _SecretResolverBase:
        return _ADAPTER.validate_python(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
        return _ADAPTER.core_schema

    @staticmethod
    def default() -> SecretResolverEnvSpec:
        """Return the default :class:`SecretResolverEnvSpec` instance."""
        return _default_secret_resolver_spec()


__all__ = [
    "SecretResolverAWSSpec",
    "SecretResolverCompositeSpec",
    "SecretResolverEnvSpec",
    "SecretResolverGCPSpec",
    "SecretResolverSpec",
]
