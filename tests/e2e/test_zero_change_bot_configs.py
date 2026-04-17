"""Zero-change middleware snapshot (Story 10 / Task 10.3).

Design context (middleware order contract): the ``harness-extensibility`` feature introduces **exactly one** new
middleware — ``IdentityResolutionMW``, at position 0.5 — and that new node
must only be inserted when the consumer opts in via ``cfg.identity_source``.

These tests encode both halves of that guarantee:

1. A pre-feature config (``examples/marketing-bot/harness.yaml`` — zero
   extension fields) produces the *baseline* middleware list. No new
   middleware may leak in without an explicit opt-in. This is the regression
   gate every existing consumer relies on.

2. Opting into ``IdentitySourceLocalFSSpec`` adds ``IdentityResolutionMW`` —
   and nothing else. This proves the feature is additive on the middleware
   axis.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness import HarnessConfig, build_universal_agent
from src.core.harness.extensions.specs.identity_source import IdentitySourceLocalFSSpec

MARKETING_BOT_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "marketing-bot" / "harness.yaml"
)

BASELINE_MIDDLEWARE: frozenset[str] = frozenset(
    {
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
    }
)


@pytest.mark.integration
def test_zero_change_config_adds_no_new_middleware() -> None:
    """Pre-feature YAML: the middleware list stays within the baseline.

    This is the "zero-change" guarantee: consumers who never opt into the new
    extension fields must see exactly the same middleware pipeline they had
    before the feature landed.
    """
    cfg = HarnessConfig.from_yaml(MARKETING_BOT_YAML)
    compiled = build_universal_agent(cfg)
    names = set(compiled.middleware_names())

    new_middleware = names - BASELINE_MIDDLEWARE
    assert new_middleware == set(), (
        f"Unexpected middleware leaked into a zero-change config: {sorted(new_middleware)}. "
        "Only IdentityResolutionMW is allowed, and only when cfg.identity_source is set."
    )
    # Every baseline middleware must still be present.
    missing = BASELINE_MIDDLEWARE - names
    assert missing == set(), f"Baseline middleware disappeared: {sorted(missing)}"


@pytest.mark.integration
def test_identity_resolution_is_the_only_new_middleware_when_opted_in() -> None:
    """Opt into ``IdentitySourceLocalFSSpec`` → exactly one new middleware is added.

    The spec (Story 10 / Task 10.3) calls this out as the definitive check
    that ``IdentityResolutionMW`` is the *only* new middleware the feature
    contributes.
    """
    cfg = HarnessConfig.from_yaml(MARKETING_BOT_YAML)
    identity_dir = MARKETING_BOT_YAML.parent / "data" / "memory"
    cfg = cfg.model_copy(
        update={
            "identity_source": IdentitySourceLocalFSSpec(
                dir=str(identity_dir),
                per_principal_subdir=False,
            )
        }
    )

    compiled = build_universal_agent(cfg)
    names = set(compiled.middleware_names())

    assert "IdentityResolutionMW" in names
    new_middleware = names - BASELINE_MIDDLEWARE
    assert new_middleware == {"IdentityResolutionMW"}, (
        f"Expected IdentityResolutionMW to be the only new middleware, got {sorted(new_middleware)}"
    )
