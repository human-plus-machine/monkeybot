"""Story 7 contract fixtures for :class:`ModelProvider` backends.

Only backends that can run without optional SDKs or network access are
exposed here. The per-backend unit suites (``test_vertex.py``,
``test_bedrock.py`` …) cover the full kwarg-forwarding matrix with
synthetic SDK stubs.

MP-C-01 requires a working :class:`BaseChatModel`, which means the
backend must either (a) have its SDK available in the test environment
or (b) supply a fake chat model that stands in for a real one. The
``FakeVertexProvider`` below wraps :class:`VertexProvider` with a
``FakeListChatModel`` swap so the contract suite runs without GCP creds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.harness.extensions.model_providers import VertexProvider
from src.core.harness.specs import AgentSpec


class _FakeVertexProvider(VertexProvider):
    """VertexProvider that returns a :class:`FakeListChatModel` in place of Vertex.

    Story 1's MP-C-01/02 invariants run against this fake so the suite
    stays hermetic. The real :class:`VertexProvider` code path (kwargs
    forwarded to :class:`ChatVertexAI`) is covered in
    ``tests/harness/extensions/model_providers/test_vertex.py``.
    """

    def build(self, spec: AgentSpec) -> Any:
        from langchain_core.language_models import FakeListChatModel

        return FakeListChatModel(responses=[f"vertex-fake:{spec.model}"])


def _vertex_factory() -> _FakeVertexProvider:
    return _FakeVertexProvider(project_id="test-project", location="us-central1")


MODEL_PROVIDER_FACTORIES: list[tuple[str, Callable[[], Any]]] = [
    ("vertex", _vertex_factory),
]

__all__ = ["MODEL_PROVIDER_FACTORIES"]
