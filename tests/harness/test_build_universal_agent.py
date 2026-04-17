"""Unit test that asserts the frozen middleware order from Phase 1B §4."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness import (
    AgentSpec,
    HarnessConfig,
    IdentitySpec,
    SandboxSpec,
    SecuritySpec,
    build_universal_agent,
)

EXPECTED_MIDDLEWARE = [
    "PrincipalPropagationMW",
    "RulesEnforcementMW",
    "RedactionMW(in)",
    "ContextPolicyMW",
    "SubagentRecursionMW",
    "ObservabilityMW",
    "CommandTierMW",
    "HITLApprovalMW",
    "ToolOutputOffloadMW",
    "RedactionMW(out)",
    "RecoveryMW",
    "ObservabilityMW",
]


def test_middleware_order(tmp_path: Path) -> None:
    cfg = HarnessConfig(
        agent=AgentSpec(name="t"),
        identity=IdentitySpec(dir=str(tmp_path), enforce_rules=False),
        security=SecuritySpec(principal_required=False),
        sandbox=SandboxSpec(backend="local_shell"),
    )
    compiled = build_universal_agent(cfg)
    names = compiled.middleware_names()
    assert names == EXPECTED_MIDDLEWARE
