"""Session-owned OpenSandbox container: one create, idle reap, attach, timings."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from monkeybot.core.tools.sandbox_executor import SandboxConfig, SandboxExecutor
from monkeybot.core.tools.sandbox_session import SandboxSession, SandboxSpec
from monkeybot.core.tools.session_jobs import SessionJobs
from testkit.sandbox import (
    CreateGate,
    _make_create_mock,
    _make_opensandbox_module,
    _mock_sandbox,
    _opensandbox_sys_modules,
)


def _config(tmp_path: Path, *, ttl: int = 30) -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        server_url="http://localhost:8080",
        api_key=None,
        image="python:3.12",
        ttl_seconds=ttl,
    )


def _opened(mock_cls: object) -> tuple[object, dict[str, object]]:
    module = _make_opensandbox_module(mock_cls)  # type: ignore[arg-type]
    return module, _opensandbox_sys_modules(module)


@pytest.mark.case("T1-SBX-01")
@pytest.mark.asyncio
async def test_concurrent_ensure_creates_once(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    gate = CreateGate(hold=True)
    gate.bind(mock_cls, sandbox)
    session = SandboxSession(_config(tmp_path), tmp_path)
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        first = asyncio.create_task(session.ensure())
        await gate.entered.wait()
        second = asyncio.create_task(session.ensure())
        await asyncio.sleep(0)
        gate.release.set()
        await first
        await second
    assert gate.count == 1
    await session.aclose()


@pytest.mark.case("T1-SBX-02")
@pytest.mark.asyncio
async def test_shared_session_survives_executor_close(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    session = SandboxSession(_config(tmp_path), tmp_path)
    jobs = SessionJobs(max_jobs=2)
    session.bind_jobs(lambda: bool(jobs.running()))
    one = SandboxExecutor(_config(tmp_path), tmp_path, session=session, jobs=jobs)
    two = SandboxExecutor(_config(tmp_path), tmp_path, session=session, jobs=jobs)
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        await one.execute("echo", ["one"])
        await two.execute("echo", ["two"])
        await one.aclose()
        await two.aclose()
    mock_cls.create.assert_awaited_once()
    sandbox.kill.assert_not_awaited()
    await session.aclose()
    sandbox.kill.assert_awaited_once()


@pytest.mark.case("T1-SBX-03")
@pytest.mark.asyncio
async def test_session_aclose_kills_once(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    session = SandboxSession(_config(tmp_path), tmp_path)
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        await session.ensure()
        await session.aclose()
        await session.aclose()
    sandbox.kill.assert_awaited_once()


@pytest.mark.case("T1-SBX-04")
@pytest.mark.asyncio
async def test_spec_change_kills_and_recreates(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    session = SandboxSession(_config(tmp_path), tmp_path)
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        await session.ensure()
        changed = SandboxSpec(
            image="other:1",
            server_url=session.spec.server_url,
            shared_filesystem=session.spec.shared_filesystem,
            use_server_proxy=session.spec.use_server_proxy,
            workspace=session.spec.workspace,
            skills=session.spec.skills,
            artifacts=session.spec.artifacts,
        )
        session.replace_spec(changed)
        await session.ensure()
    assert mock_cls.create.await_count == 2
    sandbox.kill.assert_awaited_once()
    await session.aclose()


@pytest.mark.case("T1-SBX-05")
@pytest.mark.asyncio
async def test_failed_create_is_retried(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    mock_cls.create = AsyncMock(side_effect=[RuntimeError("daemon down"), sandbox])
    session = SandboxSession(_config(tmp_path), tmp_path)
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        with pytest.raises(RuntimeError, match="daemon down"):
            await session.ensure()
        assert session.alive is False
        await session.ensure()
    assert session.alive is True
    assert mock_cls.create.await_count == 2
    await session.aclose()


@pytest.mark.case("T1-SBX-06")
@pytest.mark.asyncio
async def test_idle_reap_kills_when_quiet_and_waits_for_jobs(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    clock = {"t": 10.0}

    def now() -> float:
        return clock["t"]

    async def jump(seconds: float) -> None:
        clock["t"] += seconds

    quiet = SandboxSession(_config(tmp_path, ttl=30), tmp_path, clock=now, sleep=jump)
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        await quiet.ensure()
        assert quiet._reap_task is not None
        await quiet._reap_task
    sandbox.kill.assert_awaited_once()

    sandbox.kill.reset_mock()
    running = {"on": True}

    async def jump_then_stop(seconds: float) -> None:
        clock["t"] += seconds
        if clock["t"] > 80:
            raise asyncio.CancelledError

    busy = SandboxSession(_config(tmp_path, ttl=30), tmp_path, clock=now, sleep=jump_then_stop)
    busy.bind_jobs(lambda: running["on"])
    with patch.dict(sys.modules, modules):
        await busy.ensure()
        assert busy._reap_task is not None
        await busy._reap_task
    sandbox.kill.assert_not_awaited()


@pytest.mark.case("T1-SBX-07")
@pytest.mark.asyncio
async def test_timing_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sandbox = _mock_sandbox()
    mock_cls, _ = _make_create_mock(sandbox)
    session = SandboxSession(_config(tmp_path), tmp_path)
    executor = SandboxExecutor(_config(tmp_path), tmp_path, session=session)
    module, modules = _opened(mock_cls)
    del module
    caplog.set_level(logging.INFO)
    with patch.dict(sys.modules, modules):
        await executor.execute("echo", ["hi"])
    created = [record for record in caplog.records if hasattr(record, "sandbox_create_ms")]
    assert created
    assert created[0].sandbox_create_ms >= 0
    commands = [record for record in caplog.records if hasattr(record, "sandbox_reused")]
    assert commands
    assert commands[-1].sandbox_reused is False
    assert commands[-1].sandbox_prewarmed is False
    assert isinstance(commands[-1].sandbox_first_command_ms, float)
    await session.aclose()


@pytest.mark.case("T1-SBX-08")
@pytest.mark.asyncio
async def test_attach_connects_and_does_not_kill(tmp_path: Path) -> None:
    sandbox = _mock_sandbox()
    sandbox.id = "parent-box"
    mock_cls, _ = _make_create_mock(sandbox)
    mock_cls.connect = AsyncMock(return_value=sandbox)
    session = SandboxSession(_config(tmp_path), tmp_path, attach_id="parent-box")
    module, modules = _opened(mock_cls)
    del module
    with patch.dict(sys.modules, modules):
        await session.ensure()
        assert session.sandbox_id == "parent-box"
        await session.aclose()
    mock_cls.connect.assert_awaited()
    mock_cls.create.assert_not_called()
    sandbox.kill.assert_not_awaited()
