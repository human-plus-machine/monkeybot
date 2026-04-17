"""Google Cloud Vertex AI :class:`ModelProvider` (Story 7).

Wraps :class:`langchain_google_vertexai.ChatVertexAI`. The underlying SDK
is a core harness dependency, but the import is still lazy so that merely
importing this module is free of Vertex client setup cost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..base import ModelProvider
from ..values import ModelCapabilities

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from ...specs import AgentSpec


class VertexProvider(ModelProvider):
    """Resolve Google Cloud Vertex AI chat models.

    Args:
        project_id: Optional Google Cloud project id. ``None`` defers to
            the ``langchain_google_vertexai`` SDK's default resolution.
        location: Vertex region (defaults to ``us-central1``).
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str = "us-central1",
    ) -> None:
        self.project_id = project_id
        self.location = location

    def build(self, spec: AgentSpec) -> BaseChatModel:
        """Return a configured :class:`ChatVertexAI` for ``spec``.

        ``spec.extra_model_kwargs`` is forwarded verbatim so callers can opt
        into Vertex-specific flags (e.g. ``safety_settings``) without a
        provider subclass.
        """
        from langchain_google_vertexai import ChatVertexAI

        kwargs: dict[str, Any] = {
            "model": spec.model,
            "temperature": spec.temperature,
            "max_output_tokens": spec.max_output_tokens,
            "location": self.location,
        }
        if self.project_id is not None:
            kwargs["project"] = self.project_id
        kwargs.update(spec.extra_model_kwargs or {})
        return ChatVertexAI(**kwargs)

    def capabilities(self) -> ModelCapabilities:
        """Capabilities reflect Gemini 1.5 Pro defaults (tool-calling, vision, 1M ctx)."""
        return ModelCapabilities(
            tool_calling=True,
            streaming=True,
            thinking=False,
            vision=True,
            max_context_tokens=1_048_576,
        )


__all__ = ["VertexProvider"]
