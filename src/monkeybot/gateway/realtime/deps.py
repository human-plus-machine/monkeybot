"""Process-level dependencies for the realtime gateway.

This mirrors the SSE gateway's ``_GatewayDeps`` but is owned by the realtime package so
that the realtime app can be wired independently without modifying ``gateway/sse/app.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.realtime_provider import RealtimeProvider
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import StorageBackend
from monkeybot.core.tools.inspector import ToolInspector


@dataclass
class RealtimeDependencies:
    """Mutable process singleton populated on realtime app startup."""

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
