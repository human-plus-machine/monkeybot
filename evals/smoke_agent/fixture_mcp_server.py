"""Minimal in-process MCP fixture server for live eval scenarios (evals/scenarios/mcp/).

Runs over stdio, no network calls, no extra dependencies beyond the `mcp` package
monkeybot already depends on. Exposes one tool, one resource, and one prompt so the
mcp/ scenarios have something CI-reachable to exercise. Wired in via
evals/smoke_agent/monkeybot_config/mcp.json under the server name "fixture".
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back, prefixed with 'echo: '."""
    return f"echo: {text}"


@mcp.resource("fixture://readme")
def readme() -> str:
    """Static fixture resource used to test list_mcp_resources / read_mcp_resource."""
    return "This is a fixture MCP resource for monkeybot's live eval smoke suite."


@mcp.prompt()
def greet(name: str) -> str:
    """Fixture prompt template used to test list_mcp_prompts / get_mcp_prompt."""
    return f"Say hello to {name} in one short sentence."


if __name__ == "__main__":
    mcp.run(transport="stdio")
