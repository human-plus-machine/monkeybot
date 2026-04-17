"""OpenAI :class:`ModelProvider` (Story 7).

Wraps :class:`langchain_openai.ChatOpenAI`. The ``langchain_openai``
import is lazy; importing this module is safe even when the optional
dependency is not installed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from ..base import ModelProvider
from ..values import ModelCapabilities

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from ...specs import AgentSpec


class OpenAIProvider(ModelProvider):
    """Resolve OpenAI chat models.

    Args:
        api_key_handle: Name of the environment variable holding the
            OpenAI API key. Defaults to ``OPENAI_API_KEY``. When the
            env var is unset the SDK's own default resolution takes over.
    """

    def __init__(self, *, api_key_handle: str = "OPENAI_API_KEY") -> None:
        self.api_key_handle = api_key_handle

    def build(self, spec: AgentSpec) -> BaseChatModel:
        """Return a configured :class:`ChatOpenAI` for ``spec``.

        The API key is resolved from ``os.environ[self.api_key_handle]``
        so the provider integrates cleanly with containerised deployments
        that inject secrets via env vars. When the env var is unset the
        SDK's own default resolution takes over.
        """
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": spec.model,
            "temperature": spec.temperature,
            "max_tokens": spec.max_output_tokens,
        }
        api_key = os.environ.get(self.api_key_handle)
        if api_key:
            # Wrap the raw string in :class:`SecretStr` so pydantic-backed
            # LangChain models avoid printing the key in repr/log output.
            # Phase 6 reconciliation for the Story 6/7 verifier note that
            # "API keys flow as plain str before SDK coercion".
            kwargs["api_key"] = SecretStr(api_key)
        kwargs.update(spec.extra_model_kwargs or {})
        return ChatOpenAI(**kwargs)

    def capabilities(self) -> ModelCapabilities:
        """Capabilities reflect GPT-4o-class models (tool calling + vision, 128K ctx)."""
        return ModelCapabilities(
            tool_calling=True,
            streaming=True,
            thinking=False,
            vision=True,
            max_context_tokens=128_000,
        )


__all__ = ["OpenAIProvider"]
