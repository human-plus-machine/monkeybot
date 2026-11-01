"""T1-CTX-01 and T1-CTX-02. Stable harness prefix and volatile tail goldens."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.core.config.settings import SubagentSettings, VerifierConfig
from monkeybot.core.config.snapshot import (
    GatewayConfig,
    MemoryConfig,
    ModelConfig,
    PathsConfig,
    RuntimeConfig,
    ToolsConfig,
)
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.prompts.prompt import compose_stable_baseline, compose_volatile_tail_parts
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.todo_list.store import TodoListStore

_GOLDENS = Path(__file__).resolve().parent / "goldens" / "prompts"
_AGENT_MD = "# Contract agent\n"
_WORKSPACE = Path("/agents/demo/workspace")
_MEMORY_URI = "local:///agents/demo/memory"


class _Memory:
    def __init__(self, uri: str) -> None:
        self.uri = uri


class _FixedDate(date):
    @classmethod
    def today(cls) -> date:
        return date(2026, 10, 26)


def _runtime(env: dict[str, str]) -> RuntimeConfig:
    return RuntimeConfig(
        revision=1,
        digest="contract",
        source_path=None,
        loaded_at=0.0,
        model=ModelConfig(),
        paths=PathsConfig(),
        gateway=GatewayConfig(),
        tools=ToolsConfig(),
        memory=MemoryConfig(),
        realtime=RealtimeConfig(),
        subagents={},
        subagent_settings=SubagentSettings(),
        verifier=VerifierConfig(),
        env_values=env,
    )


def _tools(*names: str) -> list[ToolDef]:
    return [ToolDef(name, name, {}) for name in names]


@dataclass(frozen=True)
class _Variant:
    name: str
    memory: bool
    sandbox: bool
    task: bool
    mcp: tuple[str, ...]
    emission: str


# Six fixed contexts. Memory, sandbox, the task tool, the MCP catalog, and
# emission style each change the stable prefix. Terse+task also includes the
# agent-to-agent emission block; terse alone does not.
_VARIANTS = (
    _Variant("default", memory=True, sandbox=False, task=False, mcp=(), emission=""),
    _Variant("memory_off", memory=False, sandbox=False, task=False, mcp=(), emission=""),
    _Variant("sandbox_on", memory=True, sandbox=True, task=False, mcp=(), emission=""),
    _Variant("task_terse", memory=True, sandbox=False, task=True, mcp=(), emission="terse"),
    _Variant(
        "mcp_catalog", memory=True, sandbox=False, task=False, mcp=("docs", "github"), emission=""
    ),
    _Variant("emission_terse", memory=True, sandbox=False, task=False, mcp=(), emission="terse"),
)


def _context(variant: _Variant) -> TurnContext:
    env: dict[str, str] = {}
    if variant.sandbox:
        env["SANDBOX_ENABLED"] = "true"
    if variant.emission:
        env["MONKEYBOT_EMISSION_STYLE"] = variant.emission
    names = ["run_command"]
    if variant.task:
        names.append("task")
    return TurnContext(
        thread_id="thread-contract",
        request_id="request-contract",
        agent_md=_AGENT_MD,
        memory_index=[],
        skills=[],
        tools=_tools(*names),
        user_id=None,
        parent_run_id=None,
        model="contract-model",
        workspace_root=_WORKSPACE,
        memory=_Memory(_MEMORY_URI) if variant.memory else None,
        catalog_mcp_servers=variant.mcp,
        config=_runtime(env),
    )


def _commit(path: Path, text: str, *, update: bool) -> None:
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.is_file(), f"missing golden {path.name}; re-run with --update-golden"
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.case("T1-CTX-01")
@pytest.mark.parametrize("variant", _VARIANTS, ids=[item.name for item in _VARIANTS])
def test_compose_stable_baseline_matches_golden(
    variant: _Variant, request: pytest.FixtureRequest
) -> None:
    """compose_stable_baseline for one fixed TurnContext equals its committed golden."""
    text = compose_stable_baseline(_context(variant))
    if not text.endswith("\n"):
        text += "\n"
    _commit(
        _GOLDENS / f"harness_prompt.{variant.name}.txt",
        text,
        update=bool(request.config.getoption("update_golden")),
    )


@pytest.mark.case("T1-CTX-02")
async def test_compose_volatile_tail_parts_matches_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """compose_volatile_tail_parts for a fixed date, memory, skills, and todos."""
    monkeypatch.setattr("monkeybot.core.prompts.prompt.date", _FixedDate)
    store = TodoListStore("session-contract", workspace_root=tmp_path, mirror_to_disk=False)
    added = await store.add("ship the contract")
    assert not isinstance(added, str)
    ctx = TurnContext(
        thread_id="thread-contract",
        request_id="request-contract",
        agent_md=_AGENT_MD,
        memory_index=["palace has one fact"],
        skills=[SkillRef("diagnostics", "Check the agent")],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="contract-model",
        workspace_root=_WORKSPACE,
        todo_store=store,
        invoked_skill=SkillRef("diagnostics", "Check the agent"),
    )
    parts = compose_volatile_tail_parts(ctx)
    text = json.dumps(parts, indent=2, ensure_ascii=False) + "\n"
    assert set(parts) == {
        "current_date",
        "memory",
        "skills",
        "invoked_skill",
        "todos",
        "background_tasks",
        "current_request",
    }
    assert "2026-10-26" in parts["current_date"]
    assert "palace has one fact" in parts["memory"]
    assert "diagnostics" in parts["skills"]
    assert "ship the contract" in parts["todos"]
    _commit(
        _GOLDENS / "volatile_tail_parts.json",
        text,
        update=bool(request.config.getoption("update_golden")),
    )
