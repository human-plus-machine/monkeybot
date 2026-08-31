"""Process-level dependencies for the realtime gateway.

This mirrors the SSE gateway's ``GatewayRuntime`` but is owned by the realtime package so
that the realtime app can be wired independently without modifying ``gateway/sse/app.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.config.settings import SubagentConfig, normalize_model_provider
from monkeybot.core.config.snapshot import current_env, get_config_store
from monkeybot.core.context import LoopsToolRegistry
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.realtime_provider import RealtimeProvider
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import StorageBackend
from monkeybot.core.tools.inspector import ToolInspector
from monkeybot.providers.gemini_live import GeminiLiveProvider

logger = logging.getLogger(__name__)


class LivePolicySlices(Protocol):
    """Hot-reloadable policy read by a realtime turn (inspectors, tools, allowlists).

    Shared by :class:`RealtimeDependencies` and SSE ``GatewayRuntime`` so a
    rename on either side fails at type-check instead of at runtime.
    """

    inspectors: list[ToolInspector]
    hook_manager: HookManager | None
    web_search_tool: Any | None
    run_command_allowed_commands: list[str] | None
    run_command_allowed_path_prefixes: list[str] | None
    subagent_registry: dict[str, SubagentConfig]
    computer_tools: list[Any]
    computer_approvals_persist: Callable[[str, str], bool] | None


@dataclass
class RealtimeDependencies:
    """Mutable process singleton populated on realtime app startup.

    Call :meth:`freeze` after lifespan setup so accidental post-startup mutation fails
    loudly (single-process assumption; multi-replica still needs sticky WS routing).
    """

    mcp: MCPClientPort | None = None
    inspectors: list[ToolInspector] = field(default_factory=list)
    memory: MemorySubsystem | None = None
    hook_manager: HookManager | None = None
    web_search_tool: Any | None = None
    run_command_allowed_commands: list[str] | None = None
    run_command_allowed_path_prefixes: list[str] | None = None
    subagent_registry: dict[str, SubagentConfig] = field(default_factory=dict)
    realtime_provider: RealtimeProvider | None = None
    storage: StorageBackend | None = None
    attachment_store: AttachmentStore | None = None
    loops_registry: LoopsToolRegistry = field(default_factory=LoopsToolRegistry)
    computer_tools: list[Any] = field(default_factory=list)
    computer_approvals_persist: Callable[[str, str], bool] | None = None
    _frozen: bool = field(default=False, repr=False)

    def freeze(self) -> None:
        """Lock dependency fields after startup wiring completes."""
        object.__setattr__(self, "_frozen", True)

    # Derived from the protocol so a rename cannot drift from SSE's live
    # slices. ``mcp``, ``storage``, and ``memory`` stay off this list:
    # shared, mutated in place, or restart-only. ``realtime_provider`` is
    # rebuilt separately so MODEL_PROVIDER REBUILD applies to *new*
    # WebSocket connections; in-flight sessions keep the provider they
    # opened with.
    _RELOADABLE_SLICES = tuple(LivePolicySlices.__annotations__)

    def bind_realtime_provider(self) -> None:
        """Build the Live provider from the committed snapshot (bypasses freeze).

        Honors ``realtime.model.provider`` when set, else ``MODEL_PROVIDER``.
        """
        snap = get_config_store().current_or_none()
        override = snap.realtime.model.provider if snap is not None else None
        mode = normalize_model_provider(
            override or current_env("MODEL_PROVIDER", "google_vertexai")
        )
        # ponytail: rebuilt on every needs_apply reload, even when the
        # provider id is unchanged. Construction currently just stores an
        # api_key; gate on the diff if this ever does I/O.
        if mode == "google_genai":
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                logger.warning("realtime MODEL_PROVIDER=google_genai but GEMINI_API_KEY is not set")
            provider: RealtimeProvider = GeminiLiveProvider(api_key=api_key)
        else:
            provider = GeminiLiveProvider()
        object.__setattr__(self, "realtime_provider", provider)

    def sync_live_slices(self, source: Any) -> None:
        """Refresh reload-affected slices from the SSE ``GatewayRuntime`` singleton.

        Called after a successful ``POST /admin/config/reload`` so realtime
        sessions opened afterward see the same inspectors, hooks, web-search
        tool, subagent registry, and Live provider as new SSE turns, instead
        of the copy frozen at combined-app startup. Bypasses the freeze guard
        for exactly these fields; storage/audio wiring stays untouched.

        Missing names on ``source`` raise at reload time (no ``getattr``
        default) so a rename cannot silently write ``None`` into a live turn.
        """
        # ponytail: fields rebound one at a time with no lock after commit.
        # A WebSocket connecting mid-sync can observe a mix of old and new
        # slices. Tolerable under the single-process freeze() assumption.
        for name in self._RELOADABLE_SLICES:
            value = getattr(source, name)
            if isinstance(value, list):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            object.__setattr__(self, name, value)
        self.bind_realtime_provider()

    def __setattr__(self, name: str, value: Any) -> None:
        if name != "_frozen" and getattr(self, "_frozen", False):
            raise RuntimeError(f"RealtimeDependencies is frozen; cannot set {name!r} after startup")
        object.__setattr__(self, name, value)
