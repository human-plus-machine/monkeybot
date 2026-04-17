"""Agent Harness — extensibility layer (public API).

Everything a consumer needs to write, register, or resolve a new backend lives
under ``src.core.harness.extensions``. See ``docs/agent-harness.md``
and the 1b-contracts design note for the full contract.
"""

from __future__ import annotations

from .base import (
    Checkpointer,
    IdentitySource,
    JobStorage,
    MemoryStore,
    ModelProvider,
    SecretResolver,
)
from .errors import (
    BackendCapabilityMismatch,
    BackendConfigError,
    BackendNotFound,
    CheckpointerError,
    CheckpointMissing,
    IdentityNotFound,
    MemoryStoreError,
    ModelProviderError,
    SecretNotFound,
    SecretResolverError,
)

# BEGIN harness-extensibility story 5
from .identity_sources import (
    CallableIdentitySource,
    GCSIdentitySource,
    LocalFSIdentitySource,
    MongoIdentitySource,
    PostgresIdentitySource,
    S3IdentitySource,
)
from .registry import Registry, RegistryEntry, RegistrySource
# BEGIN harness-extensibility story 6
from .secret_resolvers import (
    AWSSecretsManagerResolver,
    CompositeSecretResolver,
    EnvSecretResolver,
    GCPSecretManagerResolver,
)
# END harness-extensibility story 6
from .specs import (
    CheckpointerSpec,
    IdentitySourceSpec,
    JobStorageSpec,
    MemoryStoreSpec,
    ModelProviderSpec,
    SecretResolverSpec,
)
from .values import (
    CheckpointRef,
    Item,
    LoadedIdentity,
    MemoryPatch,
    MemoryStoreCapabilities,
    ModelCapabilities,
)

# END harness-extensibility story 5

__all__ = [
    "BackendCapabilityMismatch",
    "BackendConfigError",
    "BackendNotFound",
    # BEGIN harness-extensibility story 5
    "CallableIdentitySource",
    # END harness-extensibility story 5
    "CheckpointMissing",
    "CheckpointRef",
    "Checkpointer",
    "CheckpointerError",
    "CheckpointerSpec",
    # BEGIN harness-extensibility story 5
    "GCSIdentitySource",
    # END harness-extensibility story 5
    "IdentityNotFound",
    "IdentitySource",
    "IdentitySourceSpec",
    "Item",
    "JobStorage",
    "JobStorageSpec",
    "LoadedIdentity",
    # BEGIN harness-extensibility story 5
    "LocalFSIdentitySource",
    # END harness-extensibility story 5
    "MemoryPatch",
    "MemoryStore",
    "MemoryStoreCapabilities",
    "MemoryStoreError",
    "MemoryStoreSpec",
    "ModelCapabilities",
    "ModelProvider",
    "ModelProviderError",
    "ModelProviderSpec",
    # BEGIN harness-extensibility story 5
    "MongoIdentitySource",
    "PostgresIdentitySource",
    # END harness-extensibility story 5
    "Registry",
    "RegistryEntry",
    "RegistrySource",
    # BEGIN harness-extensibility story 5
    "S3IdentitySource",
    # END harness-extensibility story 5
    "SecretNotFound",
    "SecretResolver",
    "SecretResolverError",
    "SecretResolverSpec",
    # BEGIN harness-extensibility story 6
    "AWSSecretsManagerResolver",
    "CompositeSecretResolver",
    "EnvSecretResolver",
    "GCPSecretManagerResolver",
    # END harness-extensibility story 6
]
