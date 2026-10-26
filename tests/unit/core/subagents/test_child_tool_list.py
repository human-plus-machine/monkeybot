"""T1-SUB-01: the worker's real build_context omits parent-only tools."""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.config import reset_runtime_env_state_for_tests
from monkeybot.core.context import SCHEDULED_LOOP_TOOL_NAMES, build_context
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope

_FAKE_MODEL_YAML = "model:\n  provider: fake\n  name: fake\ncomputer:\n  enabled: true\n"


@pytest.fixture(autouse=True)
def _reset_runtime_config() -> None:
    reset_runtime_env_state_for_tests()
    yield
    reset_runtime_env_state_for_tests()


@pytest.mark.case("T1-SUB-01")
@pytest.mark.asyncio
async def test_worker_build_context_omits_task_loops_and_computer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.subagents import subagent_worker

    project = tmp_path
    workspace = project / "workspace"
    workspace.mkdir()
    config_dir = project / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text(_FAKE_MODEL_YAML, encoding="utf-8")
    agent_md = config_dir / "AGENT.md"
    agent_md.write_text("# Subagent instructions\n", encoding="utf-8")
    skills = project / ".agents" / "skills"
    skills.mkdir(parents=True)

    envelope = SubagentEnvelope(
        task="say ok",
        context="",
        memory_storage_uri="",
        parent_run_id="parent-1",
        model="gemini-2.5-flash",
    )
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(project))
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_WORKSPACE", str(workspace))
    monkeypatch.setenv("AGENT_MD", "./monkeybot_config/AGENT.md")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SKILLS_PATH", str(skills))
    monkeypatch.setenv("MONKEYBOT_COMPUTER_TOOLS", "1")
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "none")
    monkeypatch.setenv(
        "MONKEYBOT_FAKE_PROVIDER_EVENTS",
        json.dumps([[{"kind": "text_delta", "text": "ok"}, {"kind": "done"}]]),
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope.to_json()))

    captured: list[list[str]] = []

    async def _record_build_context(*args: object, **kwargs: object) -> object:
        ctx = await build_context(*args, **kwargs)
        captured.append([tool.name for tool in ctx.tools])
        return ctx

    backend = MagicMock()
    backend.open = AsyncMock()
    backend.close = AsyncMock()
    backend.history.return_value = MagicMock()
    mcp = MagicMock()
    mcp.load_from_config = AsyncMock()
    mcp.disconnect_all = AsyncMock()
    mcp.wait_until_ready = AsyncMock()
    mcp.all_tools.return_value = []
    mcp.catalog_names.return_value = []
    mcp.known_server_names.return_value = []
    mcp.is_connected.return_value = False
    monkeypatch.setattr(subagent_worker, "create_storage_backend", lambda _url, **_kw: backend)
    monkeypatch.setattr(subagent_worker, "MCPClient", lambda: mcp)
    monkeypatch.setattr(subagent_worker, "build_context", _record_build_context)

    async def _fake_run_loop(*_args: object, **_kwargs: object) -> AsyncIterator[TurnComplete]:
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

    monkeypatch.setattr(subagent_worker, "run_loop", _fake_run_loop)
    monkeypatch.setattr(subagent_worker, "init_observability", lambda: False)
    monkeypatch.setattr(subagent_worker, "shutdown_observability", lambda: None)

    original = Path.cwd()
    try:
        await subagent_worker._async_main()
    finally:
        os.chdir(original)

    assert captured
    names = set(captured[0])
    assert "read_file" in names
    assert "task" not in names
    assert names.isdisjoint(SCHEDULED_LOOP_TOOL_NAMES)
    assert not any(name.startswith("computer_") for name in names)
