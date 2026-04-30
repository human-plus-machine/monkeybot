"""Unit tests for RunPackage + LocalRunPackageWriter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.core.harness.events import Principal, VersionTriple
from src.core.harness.runpackage import RunPackage
from src.core.harness.runpackage_writers import LocalRunPackageWriter


def _mkpkg(run_id: str = "run_abc") -> RunPackage:
    now = datetime.now(UTC)
    return RunPackage(
        run_id=run_id,
        session_id="s1",
        principal=Principal(kind="user", id="alice"),
        versions=VersionTriple(harness="1", deep_agents="0.1", model="gemini-2.5-flash"),
        started_at=now,
        ended_at=now,
        inputs=[{"role": "user", "content": "hi"}],
        outputs=[{"role": "assistant", "content": "hello"}],
    )


@pytest.mark.asyncio
async def test_schema_roundtrip() -> None:
    pkg = _mkpkg()
    raw = pkg.model_dump_json()
    parsed = RunPackage.model_validate_json(raw)
    assert parsed == pkg


@pytest.mark.asyncio
async def test_local_writer_write_read(tmp_path: Path) -> None:
    writer = LocalRunPackageWriter(sink_uri=str(tmp_path))
    pkg = _mkpkg()
    uri = await writer.write(pkg)
    assert uri.endswith("run_abc.json")
    loaded = await writer.read("run_abc")
    assert loaded == pkg


@pytest.mark.asyncio
async def test_writer_refuses_overwrite(tmp_path: Path) -> None:
    writer = LocalRunPackageWriter(sink_uri=str(tmp_path))
    pkg = _mkpkg()
    await writer.write(pkg)
    with pytest.raises(FileExistsError):
        await writer.write(pkg)


@pytest.mark.asyncio
async def test_writer_index(tmp_path: Path) -> None:
    writer = LocalRunPackageWriter(sink_uri=str(tmp_path))
    await writer.write(_mkpkg("r1"))
    await writer.write(_mkpkg("r2"))
    refs = await writer.index()
    ids = {r.run_id for r in refs}
    assert {"r1", "r2"}.issubset(ids)


@pytest.mark.asyncio
async def test_nested_subagent_roundtrip_json() -> None:
    now = datetime.now(UTC)
    inner = RunPackage(
        run_id="run_inner",
        session_id="s1",
        principal=Principal(kind="user", id="alice"),
        versions=VersionTriple(harness="1", deep_agents="0.1", model="gemini-2.5-flash"),
        started_at=now,
        ended_at=now,
        inputs=[{"role": "user", "content": "inner"}],
        outputs=[{"role": "assistant", "content": "inner-out"}],
    )
    outer = RunPackage(
        run_id="run_outer",
        session_id="s1",
        principal=Principal(kind="user", id="alice"),
        versions=VersionTriple(harness="1", deep_agents="0.1", model="gemini-2.5-flash"),
        started_at=now,
        ended_at=now,
        inputs=[{"role": "user", "content": "outer"}],
        outputs=[{"role": "assistant", "content": "outer-out"}],
        subagent_runs=[inner],
    )
    raw = outer.model_dump_json()
    parsed = RunPackage.model_validate_json(raw)
    assert parsed == outer
    assert len(parsed.subagent_runs) == 1
    assert parsed.subagent_runs[0].run_id == "run_inner"
