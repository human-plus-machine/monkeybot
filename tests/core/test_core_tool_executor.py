"""Tests for :class:`monkeybot.core.tools.core_tool_executor.CoreToolExecutor`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import unwrap_tool_execution_result
from monkeybot.core.workspace import create_workspace_storage


def _mem_sub(root: Path) -> MemorySubsystem:
    p = Path(root)
    p.mkdir(exist_ok=True)
    uri = "local://" + str(p.resolve())
    fake = ScriptedFakeProvider(
        [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
    )
    return MemorySubsystem(
        storage=create_workspace_storage(uri),
        provider=fake,
        model="gemini-2.5-flash",
        memory_uri=uri,
    )
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

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
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


def _stub_agent_md_for_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "AGENT.md"
    agent.write_text("# test agent\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))


@pytest.mark.asyncio
async def test_read_file_and_write_file(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    r1, e1 = unwrap_tool_execution_result(
        await ex.execute(
        call=ToolCall(call_id="1", name="read_file", args={"path": "hello.txt"}),
        ctx=ctx,
    ))
    assert e1 is not None
    err1 = json.loads(e1)
    assert err1["ok"] is False
    assert err1["error_kind"] == "validation"
    assert "Not a file" in err1["message"]

    w, ew = unwrap_tool_execution_result(
        await ex.execute(
        call=ToolCall(
            call_id="2",
            name="write_file",
            args={"path": "hello.txt", "content": "abc\n"},
        ),
        ctx=ctx,
    ))
    assert ew is None and w is not None and '"ok": true' in w

    r2, e2 = unwrap_tool_execution_result(
        await ex.execute(
        call=ToolCall(call_id="3", name="read_file", args={"path": "hello.txt", "limit": 10}),
        ctx=ctx,
    ))
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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(call_id="1", name="search_memory", args={"query": "alpha"}),
        ctx=ctx,
    ))
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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    sk = [SkillRef(name="n", description="d")]
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(call_id="1", name="list_skills", args={}),
        ctx=_ctx(skills=sk),
    ))
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["skills"] == [{"name": "n", "description": "d"}]


@pytest.mark.asyncio
async def test_run_command_cat_under_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_dir = tmp_path / "agent"
    ws = agent_dir / "workspace"
    (ws / "data" / "memory").mkdir(parents=True)
    (ws / "data" / "memory" / "f.md").write_text("inside", encoding="utf-8")
    mem = ws / "data" / "memory"
    skills = ws / "skills"
    skills.mkdir()
    monkeypatch.chdir(agent_dir)
    ex = CoreToolExecutor(
        workspace_root=ws,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["cat", "./data/memory/f.md"]},
        ),
        ctx=_ctx(),
    ))
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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["curl", "http://example.com"]},
        ),
        ctx=_ctx(),
    ))
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "policy"
    assert "curl" in payload["message"].lower() or "not allowed" in payload["message"].lower()
    assert "example_argv" in payload["details"]
    assert "allowed_commands" in payload["details"]


@pytest.mark.asyncio
async def test_run_command_uv_allowed_by_binary_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv`` is on the binary allowlist; install subcommands are blocked by deny_patterns in the loop inspector."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={"argv": ["uv", "--version"]},
            ),
            ctx=_ctx(),
        )
    )
    assert out is not None and err is None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0


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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(
            call_id="1",
            name="run_command",
            args={"argv": ["cat", "./forbidden/x.txt"]},
        ),
        ctx=_ctx(),
    ))
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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(call_id="1", name="run_command", args={}),
        ctx=_ctx(),
    ))
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "validation"
    assert "example" in payload["details"]


@pytest.mark.asyncio
async def test_unknown_tool(tmp_path: Path) -> None:
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "m"),
        skills_path=tmp_path / "s",
        mcp=_NoMCP(),
    )
    (tmp_path / "s").mkdir(exist_ok=True)
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(call_id="1", name="not_a_real_tool", args={}),
        ctx=_ctx(),
    ))
    assert out is None and err is not None
    err_obj = json.loads(err)
    assert err_obj["ok"] is False
    assert err_obj["error_kind"] == "runtime"
    assert "unknown tool" in err_obj["message"]


@pytest.mark.asyncio
async def test_task_tool_aggregates_subagent_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.runtime.events import (
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
        assert envelope.memory_storage_uri.startswith("local://")
        yield ToolCallStarted(request_id="r", tool="search", label="search", args={})
        yield ToolCallResult(request_id="r", tool="search", result="hit one", error=None)
        yield AssistantDelta(request_id="r", delta="partial")
        yield AssistantDelta(request_id="r", delta=" answer")
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=3, output_tokens=2, cached_tokens=0, cost_usd=0.0, duration_ms=10, estimated_prompt_tokens=0
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(
            call_id="c99",
            name="task",
            args={"task": "do the thing", "context": "ctx line"},
        ),
        ctx=ctx,
    ))
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["assistant_text"] == "partial answer"
    assert payload["final_message"] == "partial answer"
    assert payload["tool_call_count"] == 1
    assert payload["tool_results"] == [{"tool": "search", "snippet": "hit one"}]
    assert payload["usage"]["input_tokens"] == 3


@pytest.mark.asyncio
async def test_task_tool_resolves_subagent_type_agent_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    agents = tmp_path / "monkeybot_config" / "agents"
    agents.mkdir(parents=True)
    impl_md = agents / "researcher.md"
    impl_md.write_text("# researcher persona\n", encoding="utf-8")
    default_md = tmp_path / "monkeybot_config" / "AGENT.md"
    default_md.write_text("# parent\n", encoding="utf-8")

    seen_agent_md: list[str | None] = []

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        assert envelope.subagent_type == "researcher"
        seen_agent_md.append(envelope.agent_md)
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))

    registry = {
        "researcher": SubagentConfig(
            name="researcher",
            description="research",
            skills=[],
            agent_md="./monkeybot_config/agents/researcher.md",
        )
    }
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        subagent_registry=registry,
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-persona",
                name="task",
                args={"task": "research topic", "subagent_type": "researcher"},
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    assert seen_agent_md == [str(impl_md.resolve())]


@pytest.mark.asyncio
async def test_task_tool_unknown_subagent_type_returns_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        subagent_registry={},
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-bad",
                name="task",
                args={"task": "work", "subagent_type": "nope"},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["error_kind"] == "validation"
    assert "Unknown subagent_type" in payload["message"]


@pytest.mark.asyncio
async def test_task_tool_spawns_without_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    seen_uri: list[str] = []

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        assert envelope.memory_storage_uri == ""
        seen_uri.append(envelope.memory_storage_uri)
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="c1", name="task", args={"task": "summarize logs"}),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    assert seen_uri == [""]
    payload = json.loads(out)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_search_memory_without_memory_returns_validation_error(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="search_memory", args={"query": "alpha"}),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "validation"
    assert "memory" in payload["message"].lower()


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

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
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
        out, err = unwrap_tool_execution_result(
            await asyncio.wait_for(exec_task, timeout=5.0)
        )
    finally:
        hang.set()

    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is False
    assert any("cancelled (parent)" in e for e in payload["errors"])


@pytest.mark.asyncio
async def test_write_spill_with_inventory_writes_full_payload(tmp_path: Path) -> None:
    from monkeybot.core.tools.core_tool_executor import _write_spill_with_inventory

    body = "x" * 25_000
    out = _write_spill_with_inventory(body, tmp_path, "th1", "call-1")
    spill = tmp_path / ".monkeybot" / "spill" / "th1" / "call-1.txt"
    assert spill.read_text(encoding="utf-8") == body
    assert len(out) > len(body)
    assert "Spill inventory" in out
    assert "25000 total chars" in out
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
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx(skills=big_skills)
    out, err = unwrap_tool_execution_result(await ex.execute(call=ToolCall(call_id="c-spill", name="list_skills", args={}), ctx=ctx))
    assert err is None and out is not None
    assert len(out) > 20_000
    assert "Spill inventory" in out
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
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP())
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(await ex.execute(call=ToolCall(call_id="c1", name="list_skills", args={}), ctx=ctx))
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
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP())
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(
            call_id="r1",
            name="read_file",
            args={"path": ".monkeybot/spill/t/big.txt", "offset": 1, "limit": 10_000},
        ),
        ctx=ctx,
    ))
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["end_line"] - payload["start_line"] + 1 == 600


@pytest.mark.asyncio
async def test_read_file_non_spill_uses_workspace_defaults(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    p = root / "wide.txt"
    p.write_text("\n".join(f"L{i}" for i in range(400)), encoding="utf-8")
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP())
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(await ex.execute(
        call=ToolCall(call_id="r2", name="read_file", args={"path": "wide.txt"}),
        ctx=ctx,
    ))
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["end_line"] - payload["start_line"] + 1 <= 2000


# Removed in story-3-providers-and-snapshots: helper deleted

# ---------------------------------------------------------------------------
# Sandbox executor selection and aclose() lifecycle
# ---------------------------------------------------------------------------

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from monkeybot.core.tools.sandbox_executor import SandboxExecutor
from monkeybot.core.tools.terminal import TerminalExecutor


def _make_executor(tmp_path: Path) -> CoreToolExecutor:
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
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
            memory=_mem_sub(mem),
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
            out, err = unwrap_tool_execution_result(await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"argv": ["echo", "hello"]},
                ),
                ctx=_ctx(),
            ))

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
            out, err = unwrap_tool_execution_result(await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"argv": ["rm", "-rf", "/"]},
                ),
                ctx=_ctx(),
            ))

        # Must be an error envelope, not a successful result
        assert out is None
        assert err is not None
        payload = json.loads(err)
        assert payload.get("ok") is False or "error" in payload or "denied" in str(payload).lower()
        mock_cls.create.assert_not_called()


@pytest.mark.asyncio
async def test_task_tool_queue_mode_requires_run_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        run_store=None,
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-queue",
                name="task",
                args={"task": "do work", "context": "ctx"},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    assert "requires a configured storage backend" in err


@pytest.mark.asyncio
async def test_task_tool_queue_mode_enqueues_pending_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.persistence.durable_runs import SubagentEnvelope as StoredEnvelope
    from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    monkeypatch.setattr(
        "monkeybot.core.tools.core_tool_executor._inject_subagent_traceparent",
        lambda: "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    )

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        ex = CoreToolExecutor(
            workspace_root=root,
            memory=_mem_sub(mem),
            skills_path=skills,
            mcp=_NoMCP(),
            run_store=backend.runs(),
        )
        out, err = unwrap_tool_execution_result(
            await ex.execute(
                call=ToolCall(
                    call_id="c-enq",
                    name="task",
                    args={"task": "queued task", "context": "ctx"},
                ),
                ctx=_ctx(),
            )
        )
        assert err is None and out is not None
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["queued"] is True
        row = await backend.runs().get_run(payload["run_id"])
        assert row is not None
        assert row.status == "pending"
        stored = StoredEnvelope.from_json(row.envelope_json)
        assert stored.traceparent == "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_render_image_returns_image_block(tmp_path: Path) -> None:
    from monkeybot.core.attachments.store import FilesystemAttachmentStore
    from monkeybot.core.types.content_blocks import Image

    img_dir = tmp_path / "generated-media" / "images"
    img_dir.mkdir(parents=True)
    # minimal valid PNG for mime sniff
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
        b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    rel = "./generated-media/images/test.png"
    (tmp_path / "generated-media" / "images" / "test.png").write_bytes(png)

    store = FilesystemAttachmentStore(tmp_path)
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        attachment_store=store,
    )

    result = await ex.execute(
        call=ToolCall(call_id="ri1", name="render_image", args={"path": rel}),
        ctx=_ctx(),
    )
    assert result.error is None
    assert any(isinstance(b, Image) for b in result.blocks)
    img = next(b for b in result.blocks if isinstance(b, Image))
    assert img.mime_type == "image/png"
    assert img.metadata is not None
    assert "attachment_id" in img.metadata


@pytest.mark.asyncio
async def test_render_image_rejects_non_image(tmp_path: Path) -> None:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    ex = _make_executor(tmp_path)
    result = await ex.execute(
        call=ToolCall(call_id="ri2", name="render_image", args={"path": "./notes.txt"}),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "image" in result.error.lower()


@pytest.mark.asyncio
async def test_custom_tool_tool_execution_result_passthrough(tmp_path: Path) -> None:
    from monkeybot.core.tools.types import ToolExecutionResult
    from monkeybot.core.types.content_blocks import Image

    class _ImageTool:
        tool_def = ToolDef("emit_image", "emit test image", {"type": "object", "properties": {}})

        async def execute(self, args: dict[str, object]) -> ToolExecutionResult:
            del args
            return ToolExecutionResult.ok_blocks(
                [Image(mime_type="image/png", data="aW1n", metadata={"filename": "x.png"})]
            )

    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        extra_tools=[_ImageTool()],
    )
    result = await ex.execute(
        call=ToolCall(call_id="ct1", name="emit_image", args={}),
        ctx=_ctx(),
    )
    assert result.error is None
    assert any(isinstance(b, Image) for b in result.blocks)

