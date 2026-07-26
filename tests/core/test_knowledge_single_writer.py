"""Single-writer enforcement and read-only KnowledgeSubsystem search."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.knowledge.sqlite_index import (
    KnowledgeIndex,
    KnowledgeWriterConflictError,
)
from monkeybot.core.knowledge.subsystem import KnowledgeSubsystem
from monkeybot.core.knowledge.types import KnowledgeSettings, TextChunk
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import unwrap_tool_execution_result
from monkeybot.core.types.types_tools import ToolDef


class _NoMCP:
    async def connect(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> list[ToolDef]:
        del name, command, args, env
        return []

    async def connect_streamable_http(self, *args: Any, **kwargs: Any) -> list[ToolDef]:
        return []

    async def disconnect(self, name: str) -> None:
        del name

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no mcp")

    def all_tools(self) -> list[ToolDef]:
        return []

    def catalog_names(self) -> list[str]:
        return []


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


@pytest.mark.asyncio
async def test_second_writer_raises_while_first_open(tmp_path: Path) -> None:
    """Simulate another live PID holding the sentinel (same-process PIDs would match)."""
    db = tmp_path / "index.sqlite"
    first = KnowledgeIndex(db)
    await first.open()
    try:
        sentinel = db.with_suffix(db.suffix + ".writer-pid")
        sentinel.write_text(str(__import__("os").getpid() + 1), encoding="utf-8")
        import monkeybot.core.knowledge.sqlite_index as sqlite_index_mod

        original = sqlite_index_mod._pid_alive
        sqlite_index_mod._pid_alive = lambda _pid: True
        try:
            second = KnowledgeIndex(db)
            with pytest.raises(
                KnowledgeWriterConflictError, match="already has an active writer"
            ):
                await second.open()
        finally:
            sqlite_index_mod._pid_alive = original
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_writer_releases_sentinel_on_close(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    sentinel = db.with_suffix(db.suffix + ".writer-pid")
    first = KnowledgeIndex(db)
    await first.open()
    assert sentinel.is_file()
    await first.close()
    assert not sentinel.is_file()

    second = KnowledgeIndex(db)
    await second.open()
    try:
        assert sentinel.is_file()
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_read_only_subsystem_search_skips_flush(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
    )

    writer = await KnowledgeSubsystem.create(
        workspace_root=ws,
        settings=settings,
        knowledge_root=knowledge,
        index_path=Path(settings.index_path),
        read_only=False,
    )
    try:
        await writer._index.upsert_file(  # noqa: SLF001
            path="guide.md",
            source_type="workspace_file",
            content_hash="h1",
            mtime=1.0,
            chunks=[
                TextChunk(
                    path="guide.md",
                    source_type="workspace_file",
                    start_line=1,
                    end_line=2,
                    text="Refund policy details for customers",
                )
            ],
        )
        writer._indexer._ready = True  # noqa: SLF001

        reader = await KnowledgeSubsystem.create(
            workspace_root=ws,
            settings=settings,
            knowledge_root=knowledge,
            index_path=Path(settings.index_path),
            read_only=True,
        )
        try:
            assert reader.read_only is True
            await reader.ensure_ready()  # no-op
            await reader.flush()  # no-op
            payload = await reader.search("Refund")
            hits = payload.get("hits") or []
            assert any(h.get("path") == "guide.md" for h in hits)
            # stale key omitted when False
            assert not payload.get("stale")
        finally:
            await reader.close()
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_core_tool_executor_search_with_read_only_knowledge(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
    )

    writer = await KnowledgeSubsystem.create(
        workspace_root=ws,
        settings=settings,
        knowledge_root=knowledge,
        index_path=Path(settings.index_path),
    )
    try:
        await writer._index.upsert_file(  # noqa: SLF001
            path="a.py",
            source_type="workspace_file",
            content_hash="h",
            mtime=1.0,
            chunks=[
                TextChunk(
                    path="a.py",
                    source_type="workspace_file",
                    start_line=1,
                    end_line=1,
                    text="def authenticate_user(): pass",
                )
            ],
        )

        reader = await KnowledgeSubsystem.create(
            workspace_root=ws,
            settings=settings,
            knowledge_root=knowledge,
            index_path=Path(settings.index_path),
            read_only=True,
        )
        try:
            ex = CoreToolExecutor(
                workspace_root=ws,
                memory=None,
                skills_path=skills,
                mcp=_NoMCP(),  # type: ignore[arg-type]
                knowledge=reader,
            )
            out, err = unwrap_tool_execution_result(
                await ex.execute(
                    call=ToolCall(
                        call_id="1",
                        name="search",
                        args={"query": "authenticate"},
                    ),
                    ctx=_ctx(),
                )
            )
            assert err is None
            assert out is not None
            assert "authenticate" in out.lower()
        finally:
            await reader.close()
    finally:
        await writer.close()
