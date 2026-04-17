"""Error taxonomy for the harness extension system.

Every error in this module inherits from ``HarnessError`` so framework callers
can catch the base class and still retain access to the structured attributes.
See 1b-contracts.md §§3.1-3.6 for the per-ABC mapping.
"""

from __future__ import annotations

from ..errors import HarnessError


class BackendNotFound(HarnessError):  # noqa: N818
    """Raised when ``Registry.resolve`` cannot locate a matching factory."""

    def __init__(self, kind: str, name: str | None = None) -> None:
        self.kind = kind
        self.name = name
        detail = f"{kind}:{name}" if name else kind
        super().__init__(f"no backend found for {detail}")


class BackendConfigError(HarnessError):
    """Raised when a registered factory fails to construct or is misconfigured."""


class BackendCapabilityMismatch(HarnessError):  # noqa: N818
    """Raised when a backend lacks a capability required by the caller."""

    def __init__(self, kind: str, name: str, capability: str) -> None:
        self.kind = kind
        self.name = name
        self.capability = capability
        super().__init__(
            f"{kind}:{name} is missing required capability {capability!r}"
        )


class CheckpointerError(HarnessError):
    """Parent class for all checkpointer-surface errors."""


class CheckpointMissing(CheckpointerError):  # noqa: N818
    """Raised when a checkpoint read targets a non-existent ``checkpoint_id``."""

    def __init__(self, session_id: str, checkpoint_id: str) -> None:
        self.session_id = session_id
        self.checkpoint_id = checkpoint_id
        super().__init__(
            f"checkpoint {checkpoint_id!r} not found for session {session_id!r}"
        )


class MemoryStoreError(HarnessError):
    """Parent class for memory-store surface errors."""


class IdentityNotFound(HarnessError):  # noqa: N818
    """Raised when an ``IdentitySource.load`` call cannot resolve a principal."""

    def __init__(self, principal_id: str) -> None:
        self.principal_id = principal_id
        super().__init__(f"identity not found for principal {principal_id!r}")


class SecretNotFound(HarnessError):  # noqa: N818
    """Raised when a ``SecretResolver`` cannot resolve a handle."""

    def __init__(self, handle: str) -> None:
        self.handle = handle
        super().__init__(f"secret not found for handle {handle!r}")


class SecretResolverError(HarnessError):
    """Raised when a ``SecretResolver`` fails transport / auth before resolving."""

    def __init__(self, handle: str, reason: str | None = None) -> None:
        self.handle = handle
        self.reason = reason
        detail = f"{handle!r}" + (f": {reason}" if reason else "")
        super().__init__(f"secret resolver error for {detail}")


class ModelProviderError(HarnessError):
    """Raised when a ``ModelProvider`` cannot build a usable chat model."""
