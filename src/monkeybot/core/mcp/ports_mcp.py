"""MCP client port — structural contract for Story 5 implementation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from monkeybot.core.types.types_tools import ToolDef


def normalize_catalog_mcp_names(names: Sequence[str] | None) -> tuple[str, ...]:
    """Strip, drop empties, sort, and dedupe catalogued MCP server names."""
    return tuple(sorted({n.strip() for n in (names or ()) if n and str(n).strip()}))


@dataclass(frozen=True)
class MCPCatalogApplyResult:
    """Outcome of a live catalog diff (untouched children stay up)."""

    reconnected: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


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

    def status(self, name: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        """Return server status snapshot(s) (connected / catalogued / failed / …)."""
        ...

    async def list_resources(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List MCP resources from connected servers."""
        ...

    async def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        """Read one MCP resource by server name and URI."""
        ...

    async def list_prompts(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List MCP prompts from connected servers."""
        ...

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch a named MCP prompt (optional string arguments)."""
        ...

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        """Load mcp.json if present; no-op when path is missing (Story 5 semantics)."""
        ...

    def set_env_overlay(self, env: Mapping[str, str] | None) -> dict[str, str] | None:
        """Use a copied snapshot of env values when interpolating ``mcp.json`` ``${VAR}`` refs.

        Returns the previous overlay so callers can restore on failure.
        """
        ...

    async def apply_catalog_diff(
        self,
        mcp_json_path: Path,
        *,
        raise_on_error: bool = False,
    ) -> MCPCatalogApplyResult:
        """Reconnect only added/changed/removed servers; leave untouched children running."""
        ...
