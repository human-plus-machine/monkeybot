"""Tests for memory tree repair (quarantine + INDEX rebuild)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.context import build_context, refresh_memory_index
from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
from monkeybot.core.memory.repair import repair_memory_tree
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.workspace import create_workspace_storage
from tests.core.test_context import FakeMCPClient


def _st(root: Path):
    return create_workspace_storage("local://" + str(root.resolve()))


def _subsystem(mem_root: Path) -> MemorySubsystem:
    uri = "local://" + str(mem_root.resolve())
    fake = ScriptedFakeProvider(
        [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
    )
    return MemorySubsystem(
        storage=_st(mem_root),
        provider=fake,
        model="m",
        memory_uri=uri,
    )


def _note(body: str) -> str:
    return f"---\ntype: episodic\nstatus: active\n---\n\n{body}\n"


@pytest.mark.asyncio
async def test_repair_quarantines_corrupt_index_and_rebuilds_from_notes(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "meeting.md").write_text(
        _note("Discussed launch timeline with the team."),
        encoding="utf-8",
    )
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")

    report = await repair_memory_tree(_st(mem))

    assert report.index_rebuilt is True
    assert "INDEX.md" in report.quarantined
    assert report.entries_written == 1
    quarantine = list((mem / ".quarantine").rglob("INDEX.md"))
    assert len(quarantine) == 1
    index_text = (mem / "INDEX.md").read_text(encoding="utf-8")
    assert "[[episodic/meeting.md]]" in index_text
    assert "Discussed launch timeline" in index_text


@pytest.mark.asyncio
async def test_load_index_repairs_corrupt_utf8_instead_of_raising(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "note.md").write_text(_note("Recovered fact about cats."), encoding="utf-8")
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")

    lines = await _subsystem(mem).load_index()

    assert any("episodic/note.md" in line for line in lines)
    assert any("cats" in line for line in lines)


@pytest.mark.asyncio
async def test_fast_path_skips_note_scan_when_index_readable(tmp_path: Path) -> None:
    """Hot-path load must not open typed notes when INDEX.md is healthy."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "bad.md").write_bytes(b"\xff\xfe binary")
    (mem / "INDEX.md").write_text(
        "# Memory Index\n\n- [[episodic/bad.md]] | tags: | summary: broken\n",
        encoding="utf-8",
    )

    report = await repair_memory_tree(_st(mem))

    assert report.quarantined == []
    assert report.index_rebuilt is False
    assert (mem / "episodic" / "bad.md").exists()


@pytest.mark.asyncio
async def test_full_scan_quarantines_corrupt_note_and_prunes_index(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "good.md").write_text(_note("Healthy note body here."), encoding="utf-8")
    (mem / "episodic" / "bad.md").write_bytes(b"\xff\xfe binary")
    (mem / "INDEX.md").write_text(
        "# Memory Index\n\n"
        "- [[episodic/good.md]] | tags: | summary: Healthy note body here.\n"
        "- [[episodic/bad.md]] | tags: | summary: broken\n",
        encoding="utf-8",
    )

    report = await repair_memory_tree(_st(mem), full_scan=True)

    assert "episodic/bad.md" in report.quarantined
    assert "episodic/bad.md" in report.index_pruned
    assert not (mem / "episodic" / "bad.md").exists()
    index_text = (mem / "INDEX.md").read_text(encoding="utf-8")
    assert "episodic/good.md" in index_text
    assert "episodic/bad.md" not in index_text


@pytest.mark.asyncio
async def test_corrupt_index_without_notes_yields_empty_usable_index(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")

    lines = await _subsystem(mem).load_index()

    assert lines == []
    assert (mem / "INDEX.md").read_text(encoding="utf-8").startswith("# Memory Index")


@pytest.mark.asyncio
async def test_build_context_succeeds_with_corrupt_index(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "n.md").write_text(_note("Context still builds."), encoding="utf-8")
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )

    assert any("episodic/n.md" in line for line in ctx.memory_index)


@pytest.mark.asyncio
async def test_verify_memory_cli_repairs_before_integrity_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.verify_memory import _repair_and_report

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "n.md").write_text(_note("CLI repair path works."), encoding="utf-8")
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")

    code = await _repair_and_report(mem)

    captured = capsys.readouterr().out
    assert "Repair:" in captured
    assert "[QUARANTINED]" in captured or "quarantined" in captured.lower()
    assert (mem / "INDEX.md").read_text(encoding="utf-8").find("episodic/n.md") >= 0
    assert code == 0


@pytest.mark.asyncio
async def test_refresh_memory_index_repairs_corrupt_utf8(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "episodic").mkdir()
    (mem / "episodic" / "n.md").write_text(
        _note("Recovered after corrupt INDEX."),
        encoding="utf-8",
    )
    (mem / "INDEX.md").write_text("valid line\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")

    out = await refresh_memory_index(ctx)

    assert out is not ctx
    assert any("episodic/n.md" in line for line in out.memory_index)


@pytest.mark.asyncio
async def test_refresh_memory_index_empty_on_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("stable\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )

    async def boom(storage):
        raise OSError("simulated read failure")

    monkeypatch.setattr("monkeybot.core.memory.subsystem.async_load_index", boom)

    out = await refresh_memory_index(ctx)

    assert out is not ctx
    assert out.memory_index == []
