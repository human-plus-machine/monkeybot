"""MP-C-01 … MP-C-04 for :class:`VertexProvider`.

``langchain_google_vertexai`` is a core harness dependency so the import
is always available, but instantiating :class:`ChatVertexAI` requires
valid GCP credentials. Tests swap the class out via ``monkeypatch`` so
the provider's kwarg forwarding can be verified in isolation.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.harness.extensions import ModelProvider
from src.core.harness.extensions.model_providers import (  # noqa: F401 - register builtins
    VertexProvider,
)
from src.core.harness.specs import AgentSpec

pytest.importorskip("langchain_google_vertexai")


class _FakeChatVertexAI:
    """Capture the kwargs :class:`VertexProvider` hands to ``ChatVertexAI``."""

    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs
        self.kwargs = kwargs


@pytest.fixture
def patched_chat(monkeypatch: pytest.MonkeyPatch) -> type[_FakeChatVertexAI]:
    """Replace ``langchain_google_vertexai.ChatVertexAI`` with a capture stub."""
    import langchain_google_vertexai

    _FakeChatVertexAI.last_kwargs = None
    monkeypatch.setattr(langchain_google_vertexai, "ChatVertexAI", _FakeChatVertexAI)
    return _FakeChatVertexAI


def test_mp_c_01_build_forwards_agent_spec_kwargs(patched_chat: type[_FakeChatVertexAI]) -> None:
    """MP-C-01: ``build(spec)`` forwards model / temperature / tokens / location."""
    provider = VertexProvider(project_id="proj-123", location="europe-west4")
    agent = AgentSpec(name="a", model="gemini-2.5-flash", temperature=0.3, max_output_tokens=1024)

    provider.build(agent)

    assert patched_chat.last_kwargs is not None
    assert patched_chat.last_kwargs == {
        "model": "gemini-2.5-flash",
        "temperature": 0.3,
        "max_output_tokens": 1024,
        "location": "europe-west4",
        "project": "proj-123",
    }


def test_extra_model_kwargs_are_merged(patched_chat: type[_FakeChatVertexAI]) -> None:
    """AgentSpec.extra_model_kwargs override provider defaults."""
    provider = VertexProvider()
    agent = AgentSpec(
        name="a",
        model="gemini-2.5-flash",
        extra_model_kwargs={"safety_settings": {"HARM_CATEGORY_HATE": "BLOCK_NONE"}},
    )

    provider.build(agent)

    assert patched_chat.last_kwargs is not None
    assert patched_chat.last_kwargs["safety_settings"] == {"HARM_CATEGORY_HATE": "BLOCK_NONE"}


def test_mp_c_02_capabilities_report_tool_calling_and_vision() -> None:
    """MP-C-02: Vertex advertises tool calling and vision support."""
    caps = VertexProvider().capabilities()
    assert caps.tool_calling is True
    assert caps.vision is True
    assert caps.max_context_tokens >= 1_000_000


def test_registry_resolves_vertex_from_spec_dump() -> None:
    """The builtin registration lets the assembler resolve VertexProvider by backend name."""
    payload = {"backend": "vertex", "project_id": "p", "location": "us-central1"}
    resolved = ModelProvider.registry.resolve(payload)
    assert isinstance(resolved, VertexProvider)
    assert resolved.project_id == "p"
    assert resolved.location == "us-central1"
