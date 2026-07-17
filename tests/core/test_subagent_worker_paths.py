"""Subagent worker resolves config paths from project root, not workspace cwd."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.subagents.subagent_proto import SubagentEnvelope


@pytest.mark.asyncio
async def test_worker_resolves_relative_agent_md_from_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.subagents import subagent_worker

    project = tmp_path
    ws = project / "workspace"
    ws.mkdir()
    cfg = project / "monkeybot_config"
    cfg.mkdir()
    agent_md = cfg / "AGENT.md"
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
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("AGENT_MD", "./monkeybot_config/AGENT.md")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SKILLS_PATH", str(skills))
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv(
        "MONKEYBOT_FAKE_PROVIDER_EVENTS",
        json.dumps([[{"kind": "text_delta", "text": "ok"}, {"kind": "done"}]]),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope.to_json()))

    captured: list[Path] = []

    async def _fake_build_context(
        thread_id: str,
        request_id: str,
        *,
        agent_md_path: Path,
        **kwargs: object,
    ) -> object:
        del thread_id, request_id, kwargs
        captured.append(agent_md_path)
        from monkeybot.core.context import TurnContext

        return TurnContext(
            thread_id="t",
            request_id="r",
            agent_md=agent_md_path.read_text(encoding="utf-8"),
            memory_index=[],
            skills=[],
            tools=[],
            user_id=None,
            parent_run_id=None,
            model="gemini-2.5-flash",
        )

    backend = MagicMock()
    backend.open = AsyncMock()
    backend.close = AsyncMock()
    backend.history.return_value = MagicMock()
    monkeypatch.setattr(subagent_worker, "create_storage_backend", lambda _url: backend)

    mcp = MagicMock()
    mcp.load_from_config = AsyncMock()
    mcp.disconnect = AsyncMock()
    mcp._servers = {}
    monkeypatch.setattr(subagent_worker, "MCPClient", lambda: mcp)
    monkeypatch.setattr(subagent_worker, "build_context", _fake_build_context)

    async def _fake_run_loop(*_a: object, **_k: object):
        from monkeybot.core.runtime.events import TurnComplete, UsageTotals

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

    original_cwd = Path.cwd()
    try:
        await subagent_worker._async_main()
        assert captured == [agent_md.resolve()]
        assert Path.cwd().resolve() == ws.resolve()
    finally:
        os.chdir(original_cwd)


async def _run_worker_capturing_vertex_google_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    subagent_flag: bool,
) -> bool:
    """Runs ``_async_main`` with fakes and returns the ``vertex_google_search`` value

    the (fake) run_loop actually received, so the scoping test below can assert the
    subagent path only ever reflects its own config, never the main agent's.
    """
    from monkeybot.core.subagents import subagent_worker

    project = tmp_path
    ws = project / "workspace"
    ws.mkdir()
    cfg = project / "monkeybot_config"
    cfg.mkdir()
    agent_md = cfg / "AGENT.md"
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
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("AGENT_MD", "./monkeybot_config/AGENT.md")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SKILLS_PATH", str(skills))
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv(
        "MONKEYBOT_FAKE_PROVIDER_EVENTS",
        json.dumps([[{"kind": "text_delta", "text": "ok"}, {"kind": "done"}]]),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(envelope.to_json()))

    async def _fake_build_context(
        thread_id: str,
        request_id: str,
        *,
        agent_md_path: Path,
        **kwargs: object,
    ) -> object:
        del thread_id, request_id, kwargs
        from monkeybot.core.context import TurnContext

        return TurnContext(
            thread_id="t",
            request_id="r",
            agent_md=agent_md_path.read_text(encoding="utf-8"),
            memory_index=[],
            skills=[],
            tools=[],
            user_id=None,
            parent_run_id=None,
            model="gemini-2.5-flash",
        )

    backend = MagicMock()
    backend.open = AsyncMock()
    backend.close = AsyncMock()
    backend.history.return_value = MagicMock()
    monkeypatch.setattr(subagent_worker, "create_storage_backend", lambda _url: backend)

    mcp = MagicMock()
    mcp.load_from_config = AsyncMock()
    mcp.disconnect = AsyncMock()
    mcp._servers = {}
    monkeypatch.setattr(subagent_worker, "MCPClient", lambda: mcp)
    monkeypatch.setattr(subagent_worker, "build_context", _fake_build_context)

    monkeypatch.setattr(
        subagent_worker,
        "subagent_vertex_google_search_from_config",
        lambda *_a, **_k: subagent_flag,
    )

    def _boom(*_a: object, **_k: object) -> bool:
        raise AssertionError(
            "subagent_worker must read only its own subagents.vertex_google_search "
            "config, never the main agent's web_search.vertex_google_search"
        )

    monkeypatch.setattr(
        "monkeybot.core.config.settings.vertex_google_search_enabled_from_config", _boom
    )

    captured_kwargs: dict[str, object] = {}

    async def _fake_run_loop(*_a: object, **kwargs: object):
        captured_kwargs.update(kwargs)
        from monkeybot.core.runtime.events import TurnComplete, UsageTotals

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

    original_cwd = Path.cwd()
    try:
        await subagent_worker._async_main()
        return cast(bool, captured_kwargs["vertex_google_search"])
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent_flag", [True, False])
async def test_subagent_vertex_google_search_scoped_to_own_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subagent_flag: bool,
) -> None:
    """subagent_worker.py must pass its own ``subagents.vertex_google_search`` config

    into the run loop, and must never consult the main agent's
    ``web_search.vertex_google_search`` config function while doing so.
    """
    received = await _run_worker_capturing_vertex_google_search(
        tmp_path, monkeypatch, subagent_flag=subagent_flag
    )
    assert received is subagent_flag


def test_event_for_ndjson_pipe_redacts_system_prompt_snapshot() -> None:
    from monkeybot.core.runtime.events import AssistantDelta, SystemPromptSnapshot
    from monkeybot.core.subagents.subagent_worker import _event_for_ndjson_pipe

    snap = SystemPromptSnapshot(request_id="r", inner_turn=2, text="x" * 80_000)
    out = _event_for_ndjson_pipe(snap)
    assert isinstance(out, SystemPromptSnapshot)
    assert out.text == "[omitted 80000 chars]"
    assert out.inner_turn == 2

    delta = AssistantDelta(request_id="r", delta="hi")
    assert _event_for_ndjson_pipe(delta) is delta
