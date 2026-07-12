"""MCP client port — structural contract for Story 5 implementation."""

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from monkeybot.core.types.types_tools import ToolDef


class MCPClientPort(Protocol):
    """Async MCP session lifecycle and tool discovery (Story 5 implements this)."""

    async def connect(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> list[ToolDef]:
        """Connect server; return tools discovered for that server.

        Implementations may prefix tool names to avoid collisions.
        """
        ...

    async def connect_streamable_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        auth: object | None = None,
    ) -> list[ToolDef]:
        """Connect a remote MCP server over Streamable HTTP (``url`` in mcp.json)."""
        ...

    async def disconnect(self, name: str) -> None:
        """Disconnect and tear down the named server session."""
        ...

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: Mapping[str, object],
    ) -> str:
        """Invoke a tool on a connected server; returns serialized result text."""
        ...

    def all_tools(self) -> list[ToolDef]:
        """Aggregate tool list for the provider / tool-calling layer (sync snapshot)."""
        ...

    def catalog_names(self) -> list[str]:
        """Names of servers known from the last ``load_from_config``."""
        ...

    def known_server_names(self) -> list[str]:
        """Catalog + ever-connected server names (for tool-list refresh prefixes)."""
        ...

    def is_connected(self, name: str) -> bool:
        """True when ``name`` has an active session."""
        ...

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        """If ``prefixed_name`` belongs to a connected server, return ``(server_name, tool_name)``."""
        ...

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        """Connect a catalogued server by name (no-op if already connected)."""
        ...

    async def load_from_config(
        self, path: Path, *, raise_on_error: bool = False
    ) -> None:
        """Load mcp.json if present; no-op when path is missing (Story 5 semantics)."""
        ...
