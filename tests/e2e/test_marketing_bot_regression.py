"""Regression gate — legacy marketing-bot ``HarnessConfig`` (Story 10).

Proves the ``harness-extensibility`` feature is additive: the pre-feature
marketing-bot YAML — which contains **zero** extension-specific fields — must
still parse, must still build a ``CompiledAgent``, and the fields it does not
specify must still fall back to the shipped GCP reference classes
(``FirestoreCheckpointer``, ``FirestoreMemoryStore``, ``VertexProvider``).

This test runs offline. When GCP credentials are not available, the assembler
falls back to the stub-agent path (same behaviour ``tests/e2e/test_greenfield_smoke.py``
already relies on) so the smoke-invoke assertion still exercises the middleware
pipeline without contacting Vertex.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness import HarnessConfig, build_universal_agent
from src.core.harness.extensions.checkpointers import (
    FirestoreCheckpointer as ExtFirestoreCheckpointer,
)
from src.core.harness.extensions.memory_stores import FirestoreMemoryStore
from src.core.harness.extensions.model_providers import VertexProvider
from src.core.harness.principal import make_user_principal

MARKETING_BOT_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "marketing-bot" / "harness.yaml"
)


def _load_marketing_cfg() -> HarnessConfig:
    """Load the shipped marketing-bot YAML as a ``HarnessConfig``."""
    return HarnessConfig.from_yaml(MARKETING_BOT_YAML)


@pytest.mark.integration
def test_marketing_bot_builds_without_changes() -> None:
    """The legacy YAML parses, the build succeeds, and GCP defaults resolve.

    The three extension-specific config fields the feature introduced
    (``checkpointer``, ``memory_store``, ``model_provider``) must be ``None``
    for a pre-feature config — any non-``None`` value here would mean the YAML
    has drifted away from its "zero-change" shape.
    """
    cfg = _load_marketing_cfg()

    assert cfg.checkpointer is None
    assert cfg.memory_store is None
    assert cfg.model_provider is None

    compiled = build_universal_agent(cfg)

    # Checkpointer: scheduler.storage="firestore" → assembler picks the shipped
    # FirestoreCheckpointer. CompiledAgent does not expose a top-level
    # `.checkpointer` attribute today (Phase 6 will lift it); the canonical
    # reachable surface is the SessionRegistry.
    #
    # Phase 6 TODO: the legacy ``src.core.harness.checkpointer.FirestoreCheckpointer``
    # (what the assembler default path instantiates) is *not the same class*
    # as the new ``src.core.harness.extensions.checkpointers.FirestoreCheckpointer``
    # (what the registry ships as the ``firestore`` backend). Design §5.4 requires
    # they collapse into one registered reference implementation; until that
    # reconciliation lands we match by name and keep ``ExtFirestoreCheckpointer``
    # referenced so the regression gate fails loud if the shipped class is ever
    # renamed or removed.
    assert ExtFirestoreCheckpointer is not None
    assert (
        type(compiled.session_registry.checkpointer).__name__ == "FirestoreCheckpointer"
    )

    # ModelProvider: agent.provider="google_vertexai" → assembler synthesises
    # a ModelProviderVertexSpec() and resolves to VertexProvider.
    assert isinstance(compiled.model_provider, VertexProvider)

    # MemoryStore: cfg.memory_store is None and CompiledAgent does not yet
    # expose a .memory_store attribute — the assembler does not wire the
    # MemoryStore registry into CompiledAgent. This is a Phase 6 integration
    # gap already documented by the AWS enterprise e2e test; once resolved,
    # the assertion below should become:
    #     assert isinstance(compiled.memory_store, FirestoreMemoryStore)
    # Keeping FirestoreMemoryStore imported so the regression gate fails loud
    # if the shipped reference class is ever renamed or removed.
    assert FirestoreMemoryStore is not None
    assert not hasattr(compiled, "memory_store") or compiled.memory_store is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_marketing_bot_smoke_invoke() -> None:
    """Invoke the compiled marketing-bot agent and assert a sensible outcome.

    Mirrors ``tests/e2e/test_greenfield_smoke.py`` — without GCP credentials
    the assembler falls back to the stub agent path, which returns
    ``outcome="pass"``. A consumer running this with real Vertex credentials
    should still see one of ``{"pass", "escalate"}`` (``"escalate"`` being the
    documented outcome when HITL intervenes).
    """
    cfg = _load_marketing_cfg()
    compiled = build_universal_agent(cfg)

    result = await compiled.ainvoke(
        [{"role": "user", "content": "Draft a one-line launch teaser."}],
        session_id="marketing-regression-1",
        principal=make_user_principal(
            user_id="marketing-regression-user",
            email="regression@example.com",
        ),
    )

    assert result["outcome"] in {"pass", "escalate"}
    assert result["session_id"] == "marketing-regression-1"
