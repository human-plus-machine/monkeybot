"""Parameterized contract: any :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage`."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.workspace.protocol import WorkspaceStorage


@pytest_asyncio.fixture(
    params=[
        pytest.param("local", id="local"),
    ],
)
async def workspace_storage(tmp_path: Path, request) -> WorkspaceStorage:
    if request.param == "local":
        root = tmp_path / "contract_root"
        root.mkdir()
        return create_workspace_storage("local://" + str(root.resolve()))
    raise AssertionError(request.param)


@pytest.mark.asyncio
async def test_protocol_roundtrip(workspace_storage: WorkspaceStorage) -> None:
    await workspace_storage.write_text("p/t.txt", "hello")
    assert await workspace_storage.read_text("p/t.txt") == "hello"


@pytest.mark.asyncio
async def test_protocol_append(workspace_storage: WorkspaceStorage) -> None:
    await workspace_storage.append_text("a.log", "1")
    await workspace_storage.append_text("a.log", "2")
    assert await workspace_storage.read_text("a.log") == "12"


@pytest.mark.asyncio
async def test_protocol_move(workspace_storage: WorkspaceStorage) -> None:
    await workspace_storage.write_text("src.md", "x")
    await workspace_storage.move("src.md", "dst.md")
    assert await workspace_storage.exists("src.md") is False
    assert await workspace_storage.read_text("dst.md") == "x"
