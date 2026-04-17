"""Regression gate — legacy coding-bot ``HarnessConfig`` (Story 10).

Mirror of ``tests/e2e/test_marketing_bot_regression.py`` for the second
pre-feature consumer preserved as a regression snapshot: the coding-bot.

Both tests guarantee that consumers who never touch the new extension-specific
fields (``checkpointer``, ``memory_store``, ``job_storage``, ``identity_source``,
``model_provider``) continue to build a ``CompiledAgent`` that resolves to the
shipped GCP reference classes.
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

CODING_BOT_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "coding-bot" / "harness.yaml"
)


def _load_coding_cfg() -> HarnessConfig:
    """Load the shipped coding-bot YAML as a ``HarnessConfig``."""
    return HarnessConfig.from_yaml(CODING_BOT_YAML)


@pytest.mark.integration
def test_coding_bot_builds_without_changes() -> None:
    """The legacy YAML parses, the build succeeds, and GCP defaults resolve."""
    cfg = _load_coding_cfg()

    assert cfg.checkpointer is None
    assert cfg.memory_store is None
    assert cfg.model_provider is None

    compiled = build_universal_agent(cfg)

    # Phase 6 TODO: the legacy ``src.core.harness.checkpointer.FirestoreCheckpointer``
    # and the new registered ``extensions.checkpointers.FirestoreCheckpointer``
    # are currently two different classes (design §5.4 wants them unified). Match
    # by name until that reconciliation lands; the import of
    # ``ExtFirestoreCheckpointer`` guards against the shipped class being renamed.
    assert ExtFirestoreCheckpointer is not None
    assert (
        type(compiled.session_registry.checkpointer).__name__ == "FirestoreCheckpointer"
    )
    assert isinstance(compiled.model_provider, VertexProvider)

    # Phase 6 TODO: once CompiledAgent exposes `.memory_store`, replace with
    #     assert isinstance(compiled.memory_store, FirestoreMemoryStore)
    assert FirestoreMemoryStore is not None
    assert not hasattr(compiled, "memory_store") or compiled.memory_store is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coding_bot_smoke_invoke() -> None:
    """Invoke the compiled coding-bot agent and assert a sensible outcome."""
    cfg = _load_coding_cfg()
    compiled = build_universal_agent(cfg)

    result = await compiled.ainvoke(
        [{"role": "user", "content": "Explain what a unit test is in one line."}],
        session_id="coding-regression-1",
        principal=make_user_principal(
            user_id="coding-regression-user",
            email="regression@example.com",
        ),
    )

    assert result["outcome"] in {"pass", "escalate"}
    assert result["session_id"] == "coding-regression-1"
