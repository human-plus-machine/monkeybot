"""Tests for :class:`monkeybot.core.core_tool_executor.CoreToolExecutor`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.core_tool_executor import CoreToolExecutor
from monkeybot.core.provider import ToolCall
from monkeybot.core.types_tools import ToolDef


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

    async def connect_streamable_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[ToolDef]:
        del name, url, headers
        return []

    async def disconnect(self, name: str) -> None:
        del name

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, object],
    ) -> str:
        del server_name, tool_name, args
        return ""

    def all_tools(self) -> list[ToolDef]:
        return []

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        del prefixed_name
        return None

    async def load_from_config(self, path: Path) -> None:
        del path


def _ctx(skills: list[SkillRef] | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=skills or [],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


@pytest.mark.asyncio
async def test_read_file_and_write_file(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    r1, e1 = await ex.execute(
        call=ToolCall(call_id="1", name="read_file", args={"path": "hello.txt"}),
        ctx=ctx,
    )
    assert e1 is not None
    err1 = json.loads(e1)
    assert err1["ok"] is False
    assert err1["error_kind"] == "validation"
    assert "Not a file" in err1["message"]

    w, ew = await ex.execute(
        call=ToolCall(
            call_id="2",
            name="write_file",
            args={"path": "hello.txt", "content": "abc\n"},
        ),
        ctx=ctx,
    )
    assert ew is None and w is not None and '"ok": true' in w

    r2, e2 = await ex.execute(
        call=ToolCall(call_id="3", name="read_file", args={"path": "hello.txt", "limit": 10}),
        ctx=ctx,
    )
    assert e2 is None and r2 is not None and "abc" in r2


@pytest.mark.asyncio
async def test_search_memory(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "a.md").write_text("hello alpha world", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = await ex.execute(
        call=ToolCall(call_id="1", name="search_memory", args={"query": "alpha"}),
        ctx=ctx,
    )
    assert err is None and out is not None
    assert "alpha" in out
    assert "a.md" in out


@pytest.mark.asyncio
async def test_list_skills_uses_context(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    sk = [SkillRef(name="n", description="d")]
    out, err = await ex.execute(
        call=ToolCall(call_id="1", name="list_skills", args={}),
        ctx=_ctx(skills=sk),
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["skills"] == [{"name": "n", "description": "d"}]


@pytest.mark.asyncio
async def test_run_command_cat_under_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    (tmp_path / "data" / "memory" / "f.md").write_text("inside", encoding="utf-8")
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = await ex.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["cat", "./data/memory/f.md"]},
        ),
        ctx=_ctx(),
    )
    assert err is None and out is not None and "inside" in out


@pytest.mark.asyncio
async def test_run_command_blocked_command_returns_policy_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = await ex.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["curl", "http://example.com"]},
        ),
        ctx=_ctx(),
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "policy"
    assert "curl" in payload["message"].lower() or "not allowed" in payload["message"].lower()
    assert "example_argv" in payload["details"]
    assert "allowed_commands" in payload["details"]


@pytest.mark.asyncio
async def test_run_command_blocked_path_returns_policy_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = await ex.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["cat", "./forbidden/x.txt"]},
        ),
        ctx=_ctx(),
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "policy"
    assert "allowed_path_prefixes" in payload["details"]


@pytest.mark.asyncio
async def test_run_command_malformed_args_returns_validation_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = await ex.execute(
        call=ToolCall(call_id="1", name="run_command", args={}),
        ctx=_ctx(),
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "validation"
    assert "example" in payload["details"]


@pytest.mark.asyncio
async def test_unknown_tool(tmp_path: Path) -> None:
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=tmp_path / "m",
        skills_path=tmp_path / "s",
        mcp=_NoMCP(),
    )
    (tmp_path / "m").mkdir()
    (tmp_path / "s").mkdir()
    out, err = await ex.execute(
        call=ToolCall(call_id="1", name="not_a_real_tool", args={}),
        ctx=_ctx(),
    )
    assert out is None and err is not None
    err_obj = json.loads(err)
    assert err_obj["ok"] is False
    assert err_obj["error_kind"] == "runtime"
    assert "unknown tool" in err_obj["message"]


@pytest.mark.asyncio
async def test_task_tool_aggregates_subagent_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.events import (
        AssistantDelta,
        ToolCallResult,
        ToolCallStarted,
        TurnComplete,
        UsageTotals,
    )

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        assert envelope.task == "do the thing"
        assert "ctx line" in envelope.context
        yield ToolCallStarted(request_id="r", tool="search", label="search", args={})
        yield ToolCallResult(request_id="r", tool="search", result="hit one", error=None)
        yield AssistantDelta(request_id="r", delta="partial")
        yield AssistantDelta(request_id="r", delta=" answer")
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(input_tokens=3, output_tokens=2, cached_tokens=0, cost_usd=0.0, duration_ms=10),
        )

    monkeypatch.setattr("monkeybot.core.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = await ex.execute(
        call=ToolCall(
            call_id="c99",
            name="task",
            args={"task": "do the thing", "context": "ctx line"},
        ),
        ctx=ctx,
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["assistant_text"] == "partial answer"
    assert payload["final_message"] == "partial answer"
    assert payload["tool_call_count"] == 1
    assert payload["tool_results"] == [{"tool": "search", "snippet": "hit one"}]
    assert payload["usage"]["input_tokens"] == 3


@pytest.mark.asyncio
async def test_task_tool_parent_cancel_stops_hanging_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hang = asyncio.Event()

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        await hang.wait()
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr("monkeybot.core.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    parent_cancel = asyncio.Event()
    ctx = dataclasses.replace(_ctx(), cancelled=parent_cancel)

    exec_task = asyncio.create_task(
        ex.execute(
            call=ToolCall(
                call_id="c1",
                name="task",
                args={"task": "never finishes", "context": ""},
            ),
            ctx=ctx,
        )
    )
    await asyncio.sleep(0.05)
    parent_cancel.set()
    try:
        out, err = await asyncio.wait_for(exec_task, timeout=5.0)
    finally:
        hang.set()

    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is False
    assert any("cancelled (parent)" in e for e in payload["errors"])


@pytest.mark.asyncio
async def test_write_spill_and_cap_writes_full_payload(tmp_path: Path) -> None:
    from monkeybot.core.core_tool_executor import _write_spill_and_cap

    body = "x" * 25_000
    out = _write_spill_and_cap(body, tmp_path, "th1", "call-1")
    spill = tmp_path / ".monkeybot" / "spill" / "th1" / "call-1.txt"
    assert spill.read_text(encoding="utf-8") == body
    assert len(out) < len(body)
    assert "Full output at:" in out
    assert ".monkeybot/spill/th1/call-1.txt" in out


@pytest.mark.asyncio
async def test_list_skills_spills_large_json(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    big_skills = [
        SkillRef(name=f"s{i}", description="d" * 400) for i in range(80)
    ]
    ex = CoreToolExecutor(
        workspace_root=root,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx(skills=big_skills)
    out, err = await ex.execute(call=ToolCall(call_id="c-spill", name="list_skills", args={}), ctx=ctx)
    assert err is None and out is not None
    assert len(out) <= 20_500
    spill = root / ".monkeybot" / "spill" / "t" / "c-spill.txt"
    assert spill.is_file()
    raw = spill.read_text(encoding="utf-8")
    assert len(raw) > 20_000


@pytest.mark.asyncio
async def test_list_skills_small_no_spill(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(workspace_root=root, memory_path=mem, skills_path=skills, mcp=_NoMCP())
    ctx = _ctx()
    out, err = await ex.execute(call=ToolCall(call_id="c1", name="list_skills", args={}), ctx=ctx)
    assert err is None and out is not None
    assert not (root / ".monkeybot" / "spill").exists()


@pytest.mark.asyncio
async def test_read_file_spill_path_caps_limit(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    spill = root / ".monkeybot" / "spill" / "t" / "big.txt"
    spill.parent.mkdir(parents=True)
    spill.write_text("\n".join(f"line{i}" for i in range(600)), encoding="utf-8")
    ex = CoreToolExecutor(workspace_root=root, memory_path=mem, skills_path=skills, mcp=_NoMCP())
    ctx = _ctx()
    out, err = await ex.execute(
        call=ToolCall(
            call_id="r1",
            name="read_file",
            args={"path": ".monkeybot/spill/t/big.txt", "offset": 1, "limit": 10_000},
        ),
        ctx=ctx,
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["end_line"] - payload["start_line"] + 1 <= 500


@pytest.mark.asyncio
async def test_read_file_non_spill_uses_workspace_defaults(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    p = root / "wide.txt"
    p.write_text("\n".join(f"L{i}" for i in range(400)), encoding="utf-8")
    ex = CoreToolExecutor(workspace_root=root, memory_path=mem, skills_path=skills, mcp=_NoMCP())
    ctx = _ctx()
    out, err = await ex.execute(
        call=ToolCall(call_id="r2", name="read_file", args={"path": "wide.txt"}),
        ctx=ctx,
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["end_line"] - payload["start_line"] + 1 <= 200


# Removed in story-3-providers-and-snapshots: helper deleted

# ---------------------------------------------------------------------------
# Sandbox executor selection and aclose() lifecycle
# ---------------------------------------------------------------------------

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from monkeybot.core.sandbox_executor import SandboxConfig, SandboxExecutor
from monkeybot.core.terminal import TerminalExecutor


def _make_executor(tmp_path: Path) -> CoreToolExecutor:
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=mem,
        skills_path=skills,
        mcp=_NoMCP(),
    )


def _make_mock_sandbox_cls():
    sandbox = MagicMock()
    sandbox.id = "s1"
    sandbox.commands.run = AsyncMock(
        return_value=MagicMock(
            exit_code=0,
            logs=MagicMock(stdout=[], stderr=[]),
        )
    )
    sandbox.kill = AsyncMock()
    mock_cls = MagicMock()
    mock_cls.create = AsyncMock(return_value=sandbox)
    return mock_cls, sandbox


def _make_opensandbox_module(mock_cls):
    """Build a minimal opensandbox mock that satisfies all _ensure_sandbox imports."""
    mod = MagicMock()
    mod.Sandbox = mock_cls
    mod.config = MagicMock()
    mod.config.ConnectionConfig = MagicMock(side_effect=lambda **kw: MagicMock(**kw))

    class _Volume:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Host:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mod.models = MagicMock()
    mod.models.sandboxes = MagicMock()
    mod.models.sandboxes.Volume = _Volume
    mod.models.sandboxes.Host = _Host

    execd_mod = ModuleType("opensandbox.models.execd")

    class _RunCommandOpts:
        def __init__(
            self,
            *,
            timeout=None,
            background=False,
            working_directory=None,
            uid=None,
            gid=None,
            envs=None,
        ):
            self.timeout = timeout
            self.background = background
            self.working_directory = working_directory
            self.uid = uid
            self.gid = gid
            self.envs = envs

    execd_mod.RunCommandOpts = _RunCommandOpts
    mod.models.execd = execd_mod
    return mod


def _osb_patches(mock_cls):
    osb = _make_opensandbox_module(mock_cls)
    return osb, {
        "opensandbox": osb,
        "opensandbox.config": osb.config,
        "opensandbox.models.sandboxes": osb.models.sandboxes,
        "opensandbox.models.execd": osb.models.execd,
    }


class TestCoreToolExecutorSandboxSelection:
    """Verify that the correct executor type is chosen at init time."""

    def test_default_no_env_uses_terminal_executor(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
        ex = _make_executor(tmp_path)
        assert isinstance(ex._terminal, TerminalExecutor)

    def test_sandbox_enabled_false_uses_terminal_executor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "false")
        ex = _make_executor(tmp_path)
        assert isinstance(ex._terminal, TerminalExecutor)

    def test_sandbox_enabled_true_uses_sandbox_executor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        ex = _make_executor(tmp_path)
        assert isinstance(ex._terminal, SandboxExecutor)

    def test_explicit_terminal_injection_bypasses_sandbox_env(self, tmp_path, monkeypatch):
        # Tests that inject a terminal= override must still work regardless of env.
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        injected = TerminalExecutor()
        mem = tmp_path / "mem"
        mem.mkdir()
        skills = tmp_path / "skills"
        skills.mkdir()
        ex = CoreToolExecutor(
            workspace_root=tmp_path,
            memory_path=mem,
            skills_path=skills,
            mcp=_NoMCP(),
            terminal=injected,
        )
        assert ex._terminal is injected


class TestCoreToolExecutorAclose:
    """Verify aclose() lifecycle — no-op for TerminalExecutor, cleanup for SandboxExecutor."""

    @pytest.mark.asyncio
    async def test_aclose_with_terminal_executor_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
        ex = _make_executor(tmp_path)
        await ex.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_aclose_with_sandbox_executor_calls_sandbox_aclose(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        mock_cls, sandbox = _make_mock_sandbox_cls()
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            # Trigger sandbox creation by running a command
            await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"command": "echo hello", "argv": ["echo", "hello"]},
                ),
                ctx=_ctx(),
            )
            await ex.aclose()

        sandbox.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_twice_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        mock_cls, sandbox = _make_mock_sandbox_cls()
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"argv": ["echo", "hello"]},
                ),
                ctx=_ctx(),
            )
            await ex.aclose()
            await ex.aclose()  # second call — must be a no-op

        sandbox.kill.assert_called_once()


class TestCoreToolExecutorRunCommandWithSandbox:
    """Verify run_command tool behaviour when sandbox executor is active."""

    @pytest.mark.asyncio
    async def test_sandbox_run_command_success_returns_ok_true(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        stdout_entry = MagicMock()
        stdout_entry.text = "hello"
        mock_execution = MagicMock(
            exit_code=0,
            logs=MagicMock(stdout=[stdout_entry], stderr=[]),
        )
        sandbox = MagicMock()
        sandbox.id = "s1"
        sandbox.commands.run = AsyncMock(return_value=mock_execution)
        sandbox.kill = AsyncMock()
        mock_cls = MagicMock()
        mock_cls.create = AsyncMock(return_value=sandbox)
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            out, err = await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"argv": ["echo", "hello"]},
                ),
                ctx=_ctx(),
            )

        assert err is None
        payload = json.loads(out)
        assert payload["ok"] is True
        assert "hello" in payload["stdout"]

    @pytest.mark.asyncio
    async def test_sandbox_blocked_command_returns_error_envelope(
        self, tmp_path, monkeypatch
    ):
        # A blocked command must return a tool error envelope, NOT raise an
        # uncaught exception into the loop. Regression guard for the security
        # error -> error envelope path.
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        mock_cls = MagicMock()
        mock_cls.create = AsyncMock()  # should never be called
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            out, err = await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"argv": ["rm", "-rf", "/"]},
                ),
                ctx=_ctx(),
            )

        # Must be an error envelope, not a successful result
        assert out is None
        assert err is not None
        payload = json.loads(err)
        assert payload.get("ok") is False or "error" in payload or "denied" in str(payload).lower()
        mock_cls.create.assert_not_called()
