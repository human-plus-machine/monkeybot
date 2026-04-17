"""Contract suite invariants for every :class:`ModelProvider` backend.

IDs map to ``MP-C-01`` … ``MP-C-04`` in 1b-contracts.md §11.1.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.core.harness.extensions import ModelProvider
from src.core.harness.specs import AgentSpec

_AGENT_SPEC = AgentSpec(name="mock")


def test_mp_c_01_build_returns_usable_chat_model(
    model_provider_factory: Callable[[], ModelProvider],
) -> None:
    """MP-C-01: ``build(spec)`` returns a ``BaseChatModel`` handling a trivial invoke."""
    from langchain_core.language_models import BaseChatModel

    provider = model_provider_factory()
    model = provider.build(_AGENT_SPEC)
    assert isinstance(model, BaseChatModel)
    out = model.invoke("hello")
    assert out is not None


def test_mp_c_02_capabilities_report_tool_calling(
    model_provider_factory: Callable[[], ModelProvider],
) -> None:
    """MP-C-02: ``capabilities().tool_calling`` is truthfully reported."""
    provider = model_provider_factory()
    caps = provider.capabilities()
    assert isinstance(caps.tool_calling, bool)


@pytest.mark.skip(reason="MP-C-03 requires real Bedrock Converse schema pinning (Story 6)")
def test_mp_c_03_bedrock_schema_matches_converse(
    model_provider_factory: Callable[[], ModelProvider],
) -> None:
    """MP-C-03: Bedrock tool schema matches the published Converse shape."""


@pytest.mark.skip(reason="MP-C-04 requires streaming support beyond FakeListChatModel")
def test_mp_c_04_streaming_yields_intermediate_chunk(
    model_provider_factory: Callable[[], ModelProvider],
) -> None:
    """MP-C-04: streaming emits at least one intermediate chunk before the terminal."""
