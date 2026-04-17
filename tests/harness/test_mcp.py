"""Unit tests for load_mcp_tools."""

from __future__ import annotations

import pytest

from src.core.harness.errors import HarnessConfigError
from src.core.harness.mcp import load_mcp_tools
from src.core.harness.specs import MCPServerSpec


@pytest.mark.asyncio
async def test_empty_returns_noop() -> None:
    tools, shutdown = await load_mcp_tools([])
    assert tools == []
    await shutdown()


@pytest.mark.asyncio
async def test_missing_adapter_raises(monkeypatch) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", None)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", None)
    with pytest.raises(HarnessConfigError):
        await load_mcp_tools([MCPServerSpec(name="fs", command="echo")])
