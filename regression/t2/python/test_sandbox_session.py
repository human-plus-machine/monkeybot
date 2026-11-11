"""Gateway: one OpenSandbox container per session, jobs that outlive the turn."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.testing.mocks_provider import ScriptedProvider
from regression.t2.gateway.harness import listening, wait_turn
from testkit.gateway import open_gateway
from testkit.sandbox import (
    _make_create_mock,
    _make_opensandbox_module,
    _mock_sandbox,
    _opensandbox_sys_modules,
)

_TWO_COMMANDS = """\
- - type: tool_call
    call_id: c1
    name: run_command
    args:
      command: echo one
  - type: done
- - type: text
    text: first
  - type: done
- - type: tool_call
    call_id: c2
    name: run_command
    args:
      command: echo two
  - type: done
- - type: text
    text: second
  - type: done
"""

_NO_COMMAND = """\
- - type: text
    text: hello
  - type: done
- - type: text
    text: still here
  - type: done
"""

_BACKGROUND = """\
- - type: tool_call
    call_id: c1
    name: run_command
    args:
      argv:
      - python3
      - -c
      - "import time; time.sleep(30)"
      background: true
  - type: done
- - type: text
    text: started
  - type: done
- - type: text
    text: noted
  - type: done
- - type: tool_call
    call_id: c2
    name: await_command
    args:
      job_id: jobfixed0001
      wait_seconds: 0
  - type: done
- - type: text
    text: reported
  - type: done
"""

_PREWARM = """\
- - type: tool_call
    call_id: c1
    name: run_command
    args:
      command: echo one
  - type: done
- - type: text
    text: first
  - type: done
- - type: tool_call
    call_id: c2
    name: run_command
    args:
      command: echo two
  - type: done
- - type: text
    text: second
  - type: done
"""


def _execution(*, background: bool = False) -> MagicMock:
    execution = MagicMock()
    execution.id = "exec-1"
    execution.exit_code = None if background else 0
    execution.logs.stdout = []
    execution.logs.stderr = []
    return execution


@contextmanager
def _opensandbox(sandbox: MagicMock) -> Iterator[MagicMock]:
    mock_cls, _ = _make_create_mock(sandbox)
    module = _make_opensandbox_module(mock_cls)
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setitem(sys.modules, "opensandbox", module)
        for name, value in _opensandbox_sys_modules(module).items():
            patcher.setitem(sys.modules, name, value)
        yield mock_cls


async def _reply(gateway: Any, session_id: str, request_id: str, message: str) -> None:
    response = await gateway.post_reply(session_id, request_id, message)
    assert response.status_code == 200, response.text[:400]


def _bus(gateway: Any, session_id: str) -> Any:
    return gateway.app.state.registry.get(session_id)


@pytest.mark.case("T2-SBX-01")
@pytest.mark.asyncio
async def test_two_messages_share_one_container() -> None:
    sandbox = _mock_sandbox(_execution())
    with _opensandbox(sandbox) as mock_cls:
        async with open_gateway(
            script=_TWO_COMMANDS,
            config={"sandbox": {"enabled": True}},
            env={"SANDBOX_ENABLED": "true"},
        ) as gateway:
            await gateway.create_session("s-sbx")
            async with listening(gateway, "s-sbx") as reader:
                await _reply(gateway, "s-sbx", "r1", "run one")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-sbx")
                await _reply(gateway, "s-sbx", "r2", "run two")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-sbx")
            assert mock_cls.create.await_count == 1
            sandbox.kill.assert_not_awaited()
            deleted = await gateway.client.delete("/sessions/s-sbx")
            assert deleted.status_code in {200, 204}
    sandbox.kill.assert_awaited()


@pytest.mark.case("T2-SBX-03")
@pytest.mark.asyncio
async def test_no_container_before_a_command() -> None:
    sandbox = _mock_sandbox(_execution())
    with _opensandbox(sandbox) as mock_cls:
        async with open_gateway(
            script=_NO_COMMAND,
            config={"sandbox": {"enabled": True}},
            env={"SANDBOX_ENABLED": "true"},
        ) as gateway:
            await gateway.create_session("s-quiet")
            async with listening(gateway, "s-quiet") as reader:
                await _reply(gateway, "s-quiet", "r1", "hi")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-quiet")
                await _reply(gateway, "s-quiet", "r2", "again")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-quiet")
            session = _bus(gateway, "s-quiet").sandbox_session
            assert session is not None
            assert session.used is False
            assert session.alive is False
            mock_cls.create.assert_not_called()


@pytest.mark.case("T2-SBX-02")
@pytest.mark.asyncio
async def test_background_job_survives_turn_and_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "monkeybot.core.tools.background_jobs.new_job_id",
        lambda: "jobfixed0001",
    )
    status = MagicMock(running=True, exit_code=None, error=None)
    sandbox = _mock_sandbox(_execution(background=True))
    sandbox.commands.run = AsyncMock(return_value=_execution(background=True))
    sandbox.commands.get_command_status = AsyncMock(return_value=status)
    with _opensandbox(sandbox):
        async with open_gateway(
            script=_BACKGROUND,
            config={"sandbox": {"enabled": True}},
            env={"SANDBOX_ENABLED": "true"},
        ) as gateway:
            await gateway.create_session("s-job")
            async with listening(gateway, "s-job") as reader:
                await _reply(gateway, "s-job", "r1", "start")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-job")
                jobs = _bus(gateway, "s-job").session_jobs
                assert jobs.running(), "sandbox job was killed at turn end"
                status.running = False
                status.exit_code = 0
                wake = await reader.until("TurnComplete", timeout=15)
                assert any("noted" in str(frame) for frame in wake)
                await wait_turn(gateway, "s-job")
                await _reply(gateway, "s-job", "r2", "what happened")
                reported = await reader.until("TurnComplete")
                await wait_turn(gateway, "s-job")
            blob = "\n".join(str(frame) for frame in reported)
            assert "jobfixed0001" in blob
            assert "exit_code" in blob and "0" in blob


@pytest.mark.case("T2-SBX-04")
@pytest.mark.asyncio
async def test_prewarm_starts_before_the_provider() -> None:
    sandbox = _mock_sandbox(_execution())
    order: list[str] = []
    entered = asyncio.Event()
    release_create = asyncio.Event()
    recording = {"on": False}

    async def holding_create(*_args: object, **_kwargs: object) -> MagicMock:
        order.append("create")
        entered.set()
        await release_create.wait()
        return sandbox

    original = ScriptedProvider.stream

    def stream(self: ScriptedProvider, *args: object, **kwargs: object) -> object:
        if recording["on"]:
            order.append("provider")
        return original(self, *args, **kwargs)

    with _opensandbox(sandbox) as mock_cls:
        async with open_gateway(
            script=_PREWARM,
            config={"sandbox": {"enabled": True, "prewarm": True}},
            env={"SANDBOX_ENABLED": "true", "SANDBOX_PREWARM": "true"},
        ) as gateway:
            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(ScriptedProvider, "stream", stream)
            try:
                await gateway.create_session("s-warm")
                async with listening(gateway, "s-warm") as reader:
                    await _reply(gateway, "s-warm", "r1", "run one")
                    await reader.until("TurnComplete")
                    await wait_turn(gateway, "s-warm")
                    session = _bus(gateway, "s-warm").sandbox_session
                    assert session.used is True
                    await session.release()
                    mock_cls.create = holding_create
                    recording["on"] = True
                    reply = asyncio.create_task(_reply(gateway, "s-warm", "r2", "run two"))
                    await asyncio.wait_for(entered.wait(), timeout=10)
                    assert order[0] == "create"
                    assert "provider" not in order
                    release_create.set()
                    await reply
                    await reader.until("TurnComplete")
                    await wait_turn(gateway, "s-warm")
                assert order.index("create") < order.index("provider")
                assert order.count("create") == 1
            finally:
                monkeypatch.undo()


@pytest.mark.case("T2-SBX-05")
@pytest.mark.asyncio
async def test_delete_interrupts_then_kills() -> None:
    order: list[str] = []
    status = MagicMock(running=True, exit_code=None, error=None)
    sandbox = _mock_sandbox(_execution(background=True))
    sandbox.commands.run = AsyncMock(return_value=_execution(background=True))
    sandbox.commands.get_command_status = AsyncMock(return_value=status)

    async def interrupt(_remote_id: str) -> None:
        order.append("interrupt")

    async def kill() -> None:
        order.append("kill")

    sandbox.commands.interrupt = interrupt
    sandbox.kill = kill
    script = """\
- - type: tool_call
    call_id: c1
    name: run_command
    args:
      argv:
      - python3
      - -c
      - "import time; time.sleep(30)"
      background: true
  - type: done
- - type: text
    text: started
  - type: done
"""
    with _opensandbox(sandbox):
        async with open_gateway(
            script=script,
            config={"sandbox": {"enabled": True}},
            env={"SANDBOX_ENABLED": "true"},
        ) as gateway:
            await gateway.create_session("s-die")
            async with listening(gateway, "s-die") as reader:
                await _reply(gateway, "s-die", "r1", "start")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-die")
            deleted = await gateway.client.delete("/sessions/s-die")
            assert deleted.status_code in {200, 204}
    assert order[:2] == ["interrupt", "kill"]
