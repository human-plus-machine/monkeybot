"""Background command jobs: registry, terminal, sandbox, and tool handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monkeybot.core.context import TurnContext, _core_tool_defs
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.background_jobs import (
    JobCapExceededError,
    JobRegistry,
    JobStatus,
    jobs_check_failed_error,
    poll_background_job,
    read_log_from,
)
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.sandbox_executor import SandboxConfig, SandboxExecutor
from monkeybot.core.tools.terminal import SecurityError, TerminalExecutor
from tests.core.test_sandbox_executor import (
    _make_create_mock,
    _make_opensandbox_module,
    _opensandbox_sys_modules,
)


class _NoMCP:
    def split_prefixed_tool(self, name: str) -> None:
        del name
        return None


def _ctx(tmp_path: Path) -> TurnContext:
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
        workspace_root=tmp_path,
    )


def test_registry_cap_and_cursor(tmp_path: Path) -> None:
    registry = JobRegistry(max_jobs=1)
    from monkeybot.core.tools.background_jobs import BackgroundJob

    job = BackgroundJob(
        job_id="a",
        command="python3",
        log_path=tmp_path / "a.log",
        started_at=0,
        timeout_seconds=1,
    )
    registry.add(job)
    with pytest.raises(JobCapExceededError):
        registry.add(
            BackgroundJob(
                job_id="b",
                command="python3",
                log_path=tmp_path / "b.log",
                started_at=0,
                timeout_seconds=1,
            )
        )
    job.status = JobStatus.EXITED
    registry.add(
        BackgroundJob(
            job_id="b",
            command="python3",
            log_path=tmp_path / "b.log",
            started_at=0,
            timeout_seconds=1,
        )
    )
    path = tmp_path / "log.txt"
    path.write_text("hello world", encoding="utf-8")
    text, cursor = read_log_from(path, 0)
    assert text == "hello world"
    assert cursor == len(b"hello world")
    again, cursor2 = read_log_from(path, cursor)
    assert again == ""
    assert cursor2 == cursor


def test_read_log_from_pages_without_splitting_utf8(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_bytes(("a" + "é" * 4).encode("utf-8"))  # 1 + 4*2 bytes
    # 4-byte window ends mid-character: the cursor stops before the partial "é".
    text, cursor = read_log_from(path, 0, max_bytes=4)
    assert text == "aé"
    assert cursor == 3
    rest = ""
    while cursor < path.stat().st_size:
        chunk, cursor = read_log_from(path, cursor, max_bytes=4)
        rest += chunk
    assert text + rest == "a" + "é" * 4
    assert "\ufffd" not in text + rest


def test_read_log_from_final_flushes_trailing_partial_bytes(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_bytes(b"ok\xc3")
    text, cursor = read_log_from(path, 0)
    assert (text, cursor) == ("ok", 2)
    text, cursor = read_log_from(path, 0, final=True)
    assert text == "ok\ufffd"
    assert cursor == 3


def test_read_log_from_clamps_cursor_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    assert read_log_from(path, 5) == ("", 5)
    path.write_text("abc", encoding="utf-8")
    assert read_log_from(path, 99) == ("", 3)
    assert read_log_from(path, -4) == ("abc", 3)


def test_executors_implement_background_protocol() -> None:
    """SF adapter overrides must match this surface (see command_executor.py)."""
    required = (
        "execute",
        "start_background",
        "get_status",
        "read_output",
        "kill",
        "aclose",
        "running_jobs",
        "has_job",
    )
    for cls in (TerminalExecutor, SandboxExecutor):
        for name in required:
            assert callable(getattr(cls, name)), name


def test_await_command_tool_flags() -> None:
    tools = {tool.name: tool for tool in _core_tool_defs()}
    await_tool = tools["await_command"]
    assert await_tool.parallel_safe is True
    assert await_tool.read_only is True
    assert await_tool.doom_loop_exempt is True
    assert tools["kill_command"].name == "kill_command"
    assert "background" in tools["run_command"].input_schema["properties"]


@pytest.mark.asyncio
async def test_terminal_background_echo_await_and_cursor(tmp_path: Path) -> None:
    executor = TerminalExecutor(max_background_jobs=2, max_job_seconds=30)
    job = await executor.start_background(
        "python3",
        ["-c", "print('hello-bg')"],
        log_dir=tmp_path,
    )
    payload = await poll_background_job(executor, job.job_id, wait_seconds=10, cursor=0)
    assert payload["status"] == "exited"
    assert payload["exit_code"] == 0
    assert "hello-bg" in payload["new_output"]
    again = await poll_background_job(
        executor, job.job_id, wait_seconds=0, cursor=payload["cursor"]
    )
    assert again["new_output"] == ""
    assert await executor.aclose() == []


@pytest.mark.asyncio
async def test_terminal_background_respects_allowlist_and_cap(tmp_path: Path) -> None:
    executor = TerminalExecutor(max_background_jobs=1, max_job_seconds=30)
    with pytest.raises(SecurityError):
        await executor.start_background("rm", ["-rf", "/"], log_dir=tmp_path)
    await executor.start_background(
        "python3",
        ["-c", "import time; time.sleep(30)"],
        timeout=20,
        log_dir=tmp_path,
    )
    with pytest.raises(JobCapExceededError):
        await executor.start_background(
            "python3",
            ["-c", "import time; time.sleep(30)"],
            timeout=20,
            log_dir=tmp_path,
        )
    killed = await executor.aclose()
    assert len(killed) == 1
    assert killed[0].status in {JobStatus.KILLED, JobStatus.LOST}
    assert await executor.aclose() == []


@pytest.mark.asyncio
async def test_terminal_background_ceiling_message(tmp_path: Path) -> None:
    executor = TerminalExecutor(max_background_jobs=1, max_job_seconds=1)
    job = await executor.start_background(
        "python3",
        ["-c", "import time; time.sleep(30)"],
        timeout=100,
        log_dir=tmp_path,
    )
    payload = await poll_background_job(executor, job.job_id, wait_seconds=5, cursor=0)
    assert payload["status"] == "killed"
    assert payload["error"] == "Command exceeded the 1s maximum job time"
    await executor.aclose()


@pytest.mark.asyncio
async def test_sandbox_background_poll_renew_and_lost_status(tmp_path: Path) -> None:
    execution = MagicMock()
    execution.id = "exec-1"
    execution.exit_code = None
    execution.complete = None
    execution.logs.stdout = []
    execution.logs.stderr = []
    sandbox = MagicMock()
    sandbox.id = "sb"
    sandbox.commands.run = AsyncMock(return_value=execution)
    status = MagicMock(running=True, exit_code=None, error=None)
    sandbox.commands.get_command_status = AsyncMock(return_value=status)
    sandbox.commands.get_background_command_logs = AsyncMock(
        return_value=MagicMock(content="building\n", cursor=1)
    )
    sandbox.commands.interrupt = AsyncMock()
    sandbox.renew = AsyncMock()
    sandbox.kill = AsyncMock()
    mock_cls, _sandbox = _make_create_mock(sandbox)
    opened = _make_opensandbox_module(mock_cls)
    cfg = SandboxConfig(
        True,
        "http://localhost:8080",
        None,
        "test",
        30,
        renew_interval_seconds=0,
        max_job_seconds=60,
    )
    executor = SandboxExecutor(cfg, tmp_path)
    with patch.dict(sys.modules, _opensandbox_sys_modules(opened)):
        job = await executor.start_background("python3", ["-c", "print(1)"], log_dir=tmp_path)
        assert job.remote_id == "exec-1"
        opts = sandbox.commands.run.call_args.kwargs["opts"]
        assert opts.background is True
        assert executor._renew_task is not None
        await executor._renew_task
        sandbox.renew.assert_awaited()
        refreshed = await executor.get_status(job.job_id)
        assert refreshed.status is JobStatus.RUNNING
        text, cursor = await executor.read_output(job.job_id, 0)
        assert "building" in text
        assert cursor > 0
        status.running = False
        status.exit_code = 0
        done = await executor.get_status(job.job_id)
        assert done.status is JobStatus.EXITED
        assert done.exit_code == 0
        await executor.aclose()

    lost_status = AsyncMock(side_effect=RuntimeError("status endpoint missing"))
    sandbox.commands.get_command_status = lost_status
    execution.exit_code = None
    execution.complete = None
    running = SandboxExecutor(cfg, tmp_path)
    with patch.dict(sys.modules, _opensandbox_sys_modules(opened)):
        job = await running.start_background("python3", ["-c", "print(1)"], log_dir=tmp_path)
        stopped = await running.get_status(job.job_id)
        # Unknown status: the remote process is interrupted, not left running.
        assert stopped.status is JobStatus.KILLED
        assert "status endpoint missing" in (stopped.error or "")
        sandbox.commands.interrupt.assert_awaited_with("exec-1")
        assert running.running_jobs() == []
        await running.aclose()

    sandbox.commands.interrupt = AsyncMock(side_effect=RuntimeError("interrupt failed"))
    unreachable = SandboxExecutor(cfg, tmp_path)
    with patch.dict(sys.modules, _opensandbox_sys_modules(opened)):
        job = await unreachable.start_background("python3", ["-c", "print(1)"], log_dir=tmp_path)
        lost = await unreachable.get_status(job.job_id)
        assert lost.status is JobStatus.LOST
        assert "status endpoint missing" in (lost.error or "")
        await unreachable.aclose()


@pytest.mark.asyncio
async def test_sandbox_kill_waits_for_status_lock(tmp_path: Path) -> None:
    import asyncio

    execution = MagicMock()
    execution.id = "exec-1"
    execution.exit_code = None
    execution.complete = None
    execution.logs.stdout = []
    execution.logs.stderr = []
    sandbox = MagicMock()
    sandbox.id = "sb"
    sandbox.commands.run = AsyncMock(return_value=execution)
    sandbox.commands.interrupt = AsyncMock()
    sandbox.renew = AsyncMock()
    sandbox.kill = AsyncMock()
    mock_cls, _sandbox = _make_create_mock(sandbox)
    opened = _make_opensandbox_module(mock_cls)
    cfg = SandboxConfig(
        True,
        "http://localhost:8080",
        None,
        "test",
        30,
        renew_interval_seconds=0,
        max_job_seconds=60,
    )
    executor = SandboxExecutor(cfg, tmp_path)
    with patch.dict(sys.modules, _opensandbox_sys_modules(opened)):
        job = await executor.start_background("python3", ["-c", "print(1)"], log_dir=tmp_path)
        async with executor._lock:
            killing = asyncio.create_task(executor.kill(job.job_id))
            await asyncio.sleep(0.05)
            sandbox.commands.interrupt.assert_not_awaited()
        assert (await killing).status is JobStatus.KILLED
        sandbox.commands.interrupt.assert_awaited_once_with("exec-1")
        await executor.aclose()


@pytest.mark.asyncio
async def test_sandbox_background_allowlist_before_create(tmp_path: Path) -> None:
    mock_cls, sandbox = _make_create_mock()
    opened = _make_opensandbox_module(mock_cls)
    executor = SandboxExecutor(
        SandboxConfig(True, "http://localhost:8080", None, "test", 30),
        tmp_path,
    )
    with (
        patch.dict(sys.modules, _opensandbox_sys_modules(opened)),
        pytest.raises(SecurityError),
    ):
        await executor.start_background("rm", ["-rf", "/"])
    mock_cls.create.assert_not_awaited()
    sandbox.commands.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_command_background_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    skills = tmp_path / "skills"
    skills.mkdir()
    executor = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),  # type: ignore[arg-type]
    )
    ctx = _ctx(tmp_path)
    started = await executor.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={
                "argv": ["python3", "-c", "print('from-tool')"],
                "background": True,
                "timeout": 15,
            },
        ),
        ctx=ctx,
    )
    assert started.error is None
    body = json.loads(started.blocks[0].text)  # type: ignore[index]
    assert body["background"] is True
    waited = await executor.execute(
        call=ToolCall(
            call_id="2",
            name="await_command",
            args={"job_id": body["job_id"], "wait_seconds": 10},
        ),
        ctx=ctx,
    )
    assert waited.error is None
    done = json.loads(waited.blocks[0].text)  # type: ignore[index]
    assert done["status"] == "exited"
    assert "from-tool" in done["new_output"]
    assert done["log_path"].startswith(".monkeybot/spill/")
    unknown = await executor.execute(
        call=ToolCall(call_id="3", name="kill_command", args={"job_id": "missing"}),
        ctx=ctx,
    )
    assert unknown.error is not None
    assert "unknown background job" in unknown.error


@pytest.mark.asyncio
async def test_run_command_background_argument_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    skills = tmp_path / "skills"
    skills.mkdir()
    executor = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),  # type: ignore[arg-type]
    )
    ctx = _ctx(tmp_path)
    not_bool = await executor.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["python3", "-c", "print(1)"], "background": "true"},
        ),
        ctx=ctx,
    )
    assert not_bool.error is not None
    assert "background must be a boolean" in not_bool.error

    # An injected terminal without start_background gets a tool error, not AttributeError.
    executor._terminal = SimpleNamespace()  # type: ignore[assignment]
    unsupported = await executor.execute(
        call=ToolCall(
            call_id="2",
            name="run_command",
            args={"argv": ["python3", "-c", "print(1)"], "background": True},
        ),
        ctx=ctx,
    )
    assert unsupported.error is not None
    assert "not supported" in unsupported.error


@pytest.mark.asyncio
async def test_turn_guard_nudges_then_allows_finish() -> None:
    from monkeybot.core.llm.provider import Done, TextDelta
    from monkeybot.core.runtime.events import AssistantDelta, Error, TurnComplete
    from monkeybot.core.runtime.loop import run
    from monkeybot.core.types.content_blocks import Text
    from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, _ctx

    class _Jobs:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *, call: ToolCall, ctx: TurnContext):  # type: ignore[no-untyped-def]
            del call, ctx
            raise AssertionError("no tools expected")

        async def aclose(self) -> list[object]:
            return []

        def running_background_jobs(self) -> list[SimpleNamespace]:
            self.calls += 1
            # Call 1: pre-stream check; call 2: the turn-end check that nudges.
            if self.calls > 2:
                return []
            return [SimpleNamespace(job_id="job1", command="python3 build")]

    provider = FakeProvider(
        [
            [TextDelta(text="build started, I'll report back"), Done()],
            [TextDelta(text="build finished"), Done()],
        ]
    )
    events = []
    async for event in run(
        "build",
        _ctx(),
        provider=provider,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=_Jobs(),  # type: ignore[arg-type]
        max_turns=6,
    ):
        events.append(event)
    assert provider.stream_calls == 2
    second = provider.stream_messages[1]
    blob = "\n".join(
        block.text for message in second for block in message.content if isinstance(block, Text)
    )
    assert "await_command" in blob
    assert "job1" in blob
    assert not any(isinstance(event, Error) and "kills them" in event.error for event in events)
    # The blocked "started" reply never reaches the client; only the real answer does.
    streamed = "".join(e.delta for e in events if isinstance(e, AssistantDelta))
    assert streamed == "build finished"
    assert any(isinstance(event, TurnComplete) for event in events)


@pytest.mark.asyncio
async def test_turn_guard_fails_closed_when_job_check_raises() -> None:
    from monkeybot.core.llm.provider import Done, TextDelta
    from monkeybot.core.runtime.events import Error, TurnComplete
    from monkeybot.core.runtime.loop import run
    from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, _ctx

    class _Jobs:
        async def execute(self, *, call: ToolCall, ctx: TurnContext):  # type: ignore[no-untyped-def]
            del call, ctx
            raise AssertionError("no tools expected")

        async def aclose(self) -> list[object]:
            return []

        def running_background_jobs(self) -> list[SimpleNamespace]:
            raise RuntimeError("registry unavailable")

    provider = FakeProvider([[TextDelta(text="done"), Done()]])
    events = []
    async for event in run(
        "build",
        _ctx(),
        provider=provider,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=_Jobs(),  # type: ignore[arg-type]
        max_turns=4,
    ):
        events.append(event)
    assert provider.stream_calls == 1
    kinds = [type(event).__name__ for event in events]
    error_at = next(
        i
        for i, event in enumerate(events)
        if isinstance(event, Error) and event.error == jobs_check_failed_error()
    )
    # Held text is released before the error so the user still sees the reply.
    assert "AssistantDelta" in kinds[:error_at]
    assert any(isinstance(event, TurnComplete) for event in events)
