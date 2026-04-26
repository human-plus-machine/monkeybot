"""Regression tests for HarnessConfig fields forwarded into DeepAgents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.harness.assembler import build_universal_agent
from src.core.harness.extensions.base import MemoryStore
from src.core.harness.specs import AgentSpec, HarnessConfig, IdentitySpec


def _identity_dir(tmp_path: Path) -> Path:
    root = tmp_path / "identity"
    root.mkdir()
    return root


def test_subagents_are_forwarded_to_build_deep_agent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_deep_agent(model: Any, **kwargs: Any) -> dict[str, Any]:
        captured["model"] = model
        captured.update(kwargs)
        return {"agent": "fake"}

    monkeypatch.setattr("src.core.deepagent.build_deep_agent", fake_build_deep_agent)
    prompt_file = tmp_path / "researcher.md"
    prompt_file.write_text("You research carefully.", encoding="utf-8")
    cfg = HarnessConfig(
        agent=AgentSpec(name="harness-test"),
        identity=IdentitySpec(dir=str(_identity_dir(tmp_path)), enforce_rules=False),
        subagents=[
            {
                "name": "researcher",
                "description": "Research specialist",
                "skills": ["./skills/research"],
                "prompt_file": str(prompt_file),
                "model": "gpt-4o-mini",
                "recursion_depth_limit": 4,
            }
        ],
    )

    build_universal_agent(cfg, model="fake-model")

    assert captured["subagents"] == [
        {
            "name": "researcher",
            "description": "Research specialist",
            "system_prompt": "You research carefully.",
            "skills": ["./skills/research"],
            "model": "gpt-4o-mini",
        }
    ]


def test_memory_store_is_forwarded_as_langgraph_store(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_deep_agent(model: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"agent": "fake"}

    monkeypatch.setattr("src.core.deepagent.build_deep_agent", fake_build_deep_agent)
    cfg = HarnessConfig(
        agent=AgentSpec(name="harness-test"),
        identity=IdentitySpec(dir=str(_identity_dir(tmp_path)), enforce_rules=False),
        memory_store={"backend": "in_memory"},
    )

    compiled = build_universal_agent(cfg, model="fake-model")

    assert isinstance(compiled.memory_store, MemoryStore)
    assert captured["store"] is not None
    assert captured["store"] is not compiled.memory_store


def test_missing_memory_store_forwards_none(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_deep_agent(model: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"agent": "fake"}

    monkeypatch.setattr("src.core.deepagent.build_deep_agent", fake_build_deep_agent)
    cfg = HarnessConfig(
        agent=AgentSpec(name="harness-test"),
        identity=IdentitySpec(dir=str(_identity_dir(tmp_path)), enforce_rules=False),
    )

    build_universal_agent(cfg, model="fake-model")

    assert captured["store"] is None
    assert captured["subagents"] is None
