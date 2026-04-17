"""Local Ollama :class:`ModelProvider` (Story 7).

Wraps :class:`langchain_ollama.ChatOllama`. The ``langchain_ollama``
import is lazy; importing this module is safe even when the optional
dependency is not installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..base import ModelProvider
from ..values import ModelCapabilities

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from ...specs import AgentSpec


class OllamaProvider(ModelProvider):
    """Resolve a local Ollama chat model.

    Args:
        base_url: URL of the local Ollama daemon (default
            ``http://localhost:11434``).
    """

    def __init__(self, *, base_url: str = "http://localhost:11434") -> None:
        self.base_url = base_url

    def build(self, spec: AgentSpec) -> BaseChatModel:
        """Return a configured :class:`ChatOllama` pointed at ``self.base_url``."""
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {
            "model": spec.model,
            "temperature": spec.temperature,
            "num_predict": spec.max_output_tokens,
            "base_url": self.base_url,
        }
        kwargs.update(spec.extra_model_kwargs or {})
        return ChatOllama(**kwargs)

    def capabilities(self) -> ModelCapabilities:
        """Conservative capabilities; the true context size is model-dependent."""
        return ModelCapabilities(
            tool_calling=True,
            streaming=True,
            thinking=False,
            vision=False,
            max_context_tokens=8_192,
        )


__all__ = ["OllamaProvider"]
