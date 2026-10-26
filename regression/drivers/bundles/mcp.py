"""Real MCP client when the scenario sets ``MCP_CONFIG``."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from monkeybot.core.config.snapshot import RuntimeConfig
from monkeybot.core.context import MCP_PROGRESSIVE_META_TOOL_DEFS, _core_tool_defs
from monkeybot.core.mcp.mcp_client import MCPClient, MCPConnectionError, MCPDiagnosticError
from monkeybot.core.mcp.ports_mcp import normalize_catalog_mcp_names
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.drivers.support.paths import _REPO, _STRICT_MCP, _WORKSPACE_TOKEN
from regression.drivers.support.tokens import _expand_repo_root, _expand_tokens, _repo_tokens


def _strict_mcp_load(env: Mapping[str, str]) -> bool:
    return env.get("MCP_STRICT_LOAD", "").strip().lower() in _STRICT_MCP


def _mcp_env_overlay(cfg: RuntimeConfig, workspace: Path) -> dict[str, str]:
    """Snapshot env with ``{workspace}`` and ``{repo_root}`` already expanded.

    The child is spawned during prepare, before the driver pushes scenario
    env. ``${MB_BROWSER_STOP_MARKER}`` has to be an absolute path by then.
    """
    tokens = _repo_tokens()
    tokens[_WORKSPACE_TOKEN] = workspace.resolve().as_posix()
    overlay: dict[str, str] = {}
    for key, value in cfg.env_values.items():
        if not isinstance(value, str):
            continue
        overlay[key] = _expand_tokens(value, tokens) if "{" in value else value
    return overlay


async def _open_mcp(
    env: Mapping[str, str],
    cfg: RuntimeConfig,
    workspace: Path,
) -> tuple[MCPClient, Path]:
    """Copy the scenario MCP file into the workspace and load it.

    ``{repo_root}`` is expanded in the copy. ``MCP_STRICT_LOAD`` from the
    scenario env aborts a failed autoConnect and rolls siblings back; the
    client is still returned so the turn can observe that empty tool list.
    """
    raw = env.get("MCP_CONFIG", "").strip()
    source = Path(raw)
    if not source.is_absolute():
        source = _REPO / source
    if not source.is_file():
        raise LoopDriverError(f"MCP config not found: {source}")
    dest = workspace / "mcp.json"
    dest.write_text(_expand_repo_root(source.read_text(encoding="utf-8")), encoding="utf-8")
    client = MCPClient()
    client.set_env_overlay(_mcp_env_overlay(cfg, workspace))
    strict = _strict_mcp_load(env)
    try:
        try:
            await client.load_from_config(dest, raise_on_error=strict, wait=strict)
        except (MCPConnectionError, MCPDiagnosticError):
            if not strict:
                raise
        await client.wait_until_ready()
    except Exception:
        await client.disconnect_all()
        raise
    return client, dest


def _advertised_tools(client: MCPClient) -> tuple[list[Any], tuple[str, ...]]:
    """Core tools plus the catalog, connected MCP tools, and meta-tools."""
    catalog = normalize_catalog_mcp_names(client.catalog_names())
    tools = _core_tool_defs(include_task_tool=True, catalog_mcp_servers=catalog)
    tools.extend(client.all_tools())
    if any(client.is_connected(name) for name in client.known_server_names()):
        tools.extend(MCP_PROGRESSIVE_META_TOOL_DEFS)
    return tools, catalog


class _McpBundle(Bundle):
    order = 30

    async def prepare(self, ctx: RunCtx) -> None:
        if not ctx.env.get("MCP_CONFIG", "").strip():
            return
        if ctx.workspace is None:
            raise LoopDriverError(
                "MCP_CONFIG needs executor: real so the fixture server has a workspace"
            )
        ctx.mcp_client, ctx.mcp_path = await _open_mcp(ctx.env, ctx.cfg, ctx.workspace)
        ctx.advertised, ctx.catalog = _advertised_tools(ctx.mcp_client)

    async def close(self, ctx: RunCtx) -> None:
        if ctx.mcp_client is None:
            return
        await ctx.mcp_client.disconnect_all()
        ctx.mcp_client = None


BUNDLE = _McpBundle()
