"""Frozen Pydantic value types used by the extension ABCs.

All models live here (and not inside ``base.py``) so backends can import them
without pulling in the abstract base classes. See 1b-contracts.md §§3.1-3.6.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

try:
    from langgraph.store.base import Item as _LangGraphItem
except ImportError:  # pragma: no cover - langgraph is a runtime dependency
    _LangGraphItem = None  # type: ignore[assignment]


class CheckpointRef(BaseModel):
    """Immutable handle to a written checkpoint.

    Attributes:
        session_id: Owning session identifier.
        checkpoint_id: Monotonic identifier, unique within ``session_id``.
        reason: Why the checkpoint was written.
        created_at: UTC timestamp of the write.
        bytes: Size of the serialized payload in bytes.
        uri: Backend-specific pointer used for re-reading the payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    checkpoint_id: str
    reason: Literal["turn_end", "pre_destructive", "manual", "rewind"]
    created_at: datetime
    bytes: int
    uri: str


class MemoryStoreCapabilities(BaseModel):
    """Reported capabilities of a ``MemoryStore`` backend.

    The assembler reads this model to decide whether a config-level requirement
    (for example ``require_vector_search=True``) is satisfiable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    vector_search: bool = False
    keyword_search: bool = True
    namespace_listing: bool = True
    ttl: bool = False
    transactional: bool = False


class ModelCapabilities(BaseModel):
    """Reported capabilities of a ``ModelProvider`` backend.

    Used by the assembler to validate the harness/tooling configuration against
    the selected model's abilities (tool calling, streaming, thinking, vision).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_calling: bool = True
    streaming: bool = True
    thinking: bool = False
    vision: bool = False
    max_context_tokens: int = 128_000


class MemoryPatch(BaseModel):
    """Structured edit applied to identity-backed memory files.

    Attributes:
        target: Which identity file is being edited.
        operation: ``append``, ``replace`` or ``delete``.
        content: Required for ``append`` and ``replace``; must be ``None`` for
            ``delete``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    target: Literal["MEMORY.md", "HEARTBEAT.md"]
    operation: Literal["append", "replace", "delete"]
    content: str | None = None


class LoadedIdentity(BaseModel):
    """Frozen projection of every identity artefact an agent node needs.

    Instantiated by an ``IdentitySource`` at the start of a turn and surfaced
    to downstream middleware through ``ctx["identity"]``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    principal_id: str
    session_id: str | None = None
    soul: str = ""
    rules: str = ""
    identity: str = ""
    user: str = ""
    index: str = ""
    memory: str = ""
    heartbeat: str = ""
    loaded_at: datetime
    ttl_seconds: int = 300
    source_backend: str
    extras: Mapping[str, str] = Field(default_factory=dict)

    # BEGIN harness-extensibility story 5
    def system_prompt_block(self) -> str:
        """Compose identity files into a deterministic block for the system prompt.

        Sections are emitted in the canonical order (SOUL → IDENTITY → USER →
        INDEX → RULES → MEMORY → HEARTBEAT). Empty/whitespace-only sections are
        dropped so the resulting block is stable across loads.
        """
        sections: list[str] = []
        for label, body in (
            ("SOUL", self.soul),
            ("IDENTITY", self.identity),
            ("USER", self.user),
            ("INDEX", self.index),
            ("RULES", self.rules),
            ("MEMORY", self.memory),
            ("HEARTBEAT", self.heartbeat),
        ):
            if body.strip():
                sections.append(f"# === {label} ===\n{body.strip()}")
        return "\n\n".join(sections)
    # END harness-extensibility story 5


if _LangGraphItem is not None:
    Item = _LangGraphItem
else:  # pragma: no cover - fallback only hit when langgraph is missing
    class Item(BaseModel):  # type: ignore[no-redef]
        """Fallback ``Item`` shape (used only if langgraph is not installed)."""

        model_config = ConfigDict(extra="forbid", frozen=True)
        namespace: tuple[str, ...]
        key: str
        value: Mapping[str, Any]
        created_at: datetime
        updated_at: datetime


__all__ = [
    "CheckpointRef",
    "Item",
    "LoadedIdentity",
    "MemoryPatch",
    "MemoryStoreCapabilities",
    "ModelCapabilities",
]
