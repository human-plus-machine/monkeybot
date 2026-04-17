"""Unit tests for HarnessConfig Pydantic schemas."""

from __future__ import annotations

import pytest
import yaml

from src.core.harness import HarnessConfig
from src.core.harness.specs import (
    AgentSpec,
    ContextPolicySpec,
    HITLSpec,
    MCPServerSpec,
    SandboxSpec,
)


def test_minimal_config_builds_with_defaults() -> None:
    cfg = HarnessConfig(agent=AgentSpec(name="test-agent"))
    assert cfg.agent.name == "test-agent"
    assert cfg.agent.provider == "google_vertexai"
    assert cfg.context.summarize_at == 0.75
    assert cfg.context.hard_reset_at == 0.92
    assert cfg.version == "1"


def test_extra_fields_rejected() -> None:
    with pytest.raises(Exception):
        HarnessConfig.model_validate(
            {"version": "1", "agent": {"name": "x"}, "unexpected": True}
        )


def test_context_threshold_validator() -> None:
    with pytest.raises(Exception):
        ContextPolicySpec(summarize_at=0.9, hard_reset_at=0.8)


def test_hitl_webhook_requires_url() -> None:
    with pytest.raises(Exception):
        HITLSpec(channel="webhook", webhook_url=None)


def test_sandbox_custom_requires_import_path() -> None:
    with pytest.raises(Exception):
        SandboxSpec(backend="custom", custom_import_path=None)


def test_mcp_stdio_requires_command() -> None:
    with pytest.raises(Exception):
        MCPServerSpec(name="x", transport="stdio")


def test_yaml_roundtrip(tmp_path) -> None:
    path = tmp_path / "h.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "agent": {"name": "demo"},
                "identity": {"enforce_rules": False},
            }
        )
    )
    cfg = HarnessConfig.from_yaml(path)
    assert cfg.agent.name == "demo"
    assert cfg.identity.enforce_rules is False
    out = tmp_path / "out.yaml"
    cfg.to_yaml(out)
    reloaded = HarnessConfig.from_yaml(out)
    assert reloaded == cfg
