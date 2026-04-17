"""MCP adapter — thin wrapper around langchain-mcp-adapters.MultiServerMCPClient.

Returns LangChain BaseTools indistinguishable from native tools at the middleware layer.
Consumers declare servers in HarnessConfig.mcp_servers.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence

from .errors import HarnessConfigError
from .specs import MCPServerSpec


async def load_mcp_tools(
    specs: Sequence[MCPServerSpec],
) -> tuple[list, Callable[[], Awaitable[None]]]:
    """Load MCP tools from a list of server specs.

    Returns ``(tools, shutdown)`` where ``shutdown`` closes all MCP sessions.
    If ``specs`` is empty, returns ``([], no-op-shutdown)``.
    Raises ``HarnessConfigError`` if langchain-mcp-adapters is not installed but
    servers are configured.
    """
    if not specs:
        async def _noop() -> None:
            return None

        return [], _noop

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore
    except ImportError as exc:
        raise HarnessConfigError(
            "mcp_servers configured but langchain-mcp-adapters not installed. "
            "Install with: pip install 'emonk[mcp]'"
        ) from exc

    client_config: dict = {}
    for spec in specs:
        if spec.transport == "stdio":
            client_config[spec.name] = {
                "command": spec.command,
                "args": spec.args,
                "env": spec.env,
                "transport": "stdio",
            }
        else:
            client_config[spec.name] = {
                "url": spec.url,
                "transport": spec.transport,
            }

    client = MultiServerMCPClient(client_config)
    tools = await client.get_tools()

    async def _shutdown() -> None:
        if hasattr(client, "close"):
            await client.close()
        elif hasattr(client, "aclose"):
            await client.aclose()

    return list(tools), _shutdown
