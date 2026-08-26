"""Process-level dependencies for the realtime gateway.

This mirrors the SSE gateway's ``_GatewayDeps`` but is owned by the realtime package so
that the realtime app can be wired independently without modifying ``gateway/sse/app.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.context import LoopsToolRegistry
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.realtime_provider import RealtimeProvider
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import StorageBackend
from monkeybot.core.tools.inspector import ToolInspector


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

    # Fields the SSE ``GatewayRuntime.apply()`` replaces wholesale (rebinds a
    # new object) rather than mutating in place. ``mcp``, ``storage``,
    # ``realtime_provider``, and ``memory`` are excluded: they are either
    # shared, mutated-in-place objects (so the existing reference stays
    # valid) or intentionally restart-only for realtime.
    _RELOADABLE_SLICES = (
        "inspectors",
        "hook_manager",
        "web_search_tool",
        "run_command_allowed_commands",
        "run_command_allowed_path_prefixes",
        "subagent_registry",
        "computer_tools",
        "computer_approvals_persist",
    )

    def sync_live_slices(self, source: Any) -> None:
        """Refresh reload-affected slices from the SSE ``GatewayRuntime`` singleton.

        Called after a successful ``POST /admin/config/reload`` so realtime
        sessions opened afterward see the same inspectors, hooks, web-search
        tool, and subagent registry as new SSE turns, instead of the copy
        frozen at combined-app startup. Bypasses the freeze guard for exactly
        these fields; provider/storage/audio wiring stays untouched.
        """
        for name in self._RELOADABLE_SLICES:
            value = getattr(source, name, None)
            if isinstance(value, list):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        if name != "_frozen" and getattr(self, "_frozen", False):
            raise RuntimeError(
                f"RealtimeDependencies is frozen; cannot set {name!r} after startup"
            )
        object.__setattr__(self, name, value)
