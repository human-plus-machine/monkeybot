"""Builtin :class:`ModelProvider` backends + registry wiring (Story 7).

Importing this package registers the five shipped foundation-model
backends (``vertex``, ``bedrock``, ``openai``, ``anthropic``, ``ollama``)
against :class:`ModelProvider.registry`. The underlying SDKs
(``langchain_aws``, ``langchain_openai``, ``langchain_anthropic``,
``langchain_ollama``, ``langchain_google_vertexai``) are imported lazily
inside each concrete provider so importing this package is always cheap
and never raises when an optional extra is not installed.
"""

from __future__ import annotations

import contextlib

from ..base import ModelProvider
from ..errors import BackendConfigError
from .anthropic import AnthropicProvider
from .bedrock import BedrockProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .vertex import VertexProvider


def _register_once(name: str, factory: type[ModelProvider]) -> None:
    """Register ``factory`` under ``name`` if not already registered as a builtin."""
    existing = ModelProvider.registry.entry(name)
    if existing is not None and existing.source == "builtin":
        return
    with contextlib.suppress(BackendConfigError):  # pragma: no cover - defensive
        ModelProvider.registry.register(name, factory, source="builtin")


_register_once("vertex", VertexProvider)
_register_once("bedrock", BedrockProvider)
_register_once("openai", OpenAIProvider)
_register_once("anthropic", AnthropicProvider)
_register_once("ollama", OllamaProvider)


__all__ = [
    "AnthropicProvider",
    "BedrockProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "VertexProvider",
]
