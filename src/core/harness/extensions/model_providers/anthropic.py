"""Anthropic :class:`ModelProvider` (Story 7).

Wraps :class:`langchain_anthropic.ChatAnthropic`. The ``langchain_anthropic``
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


class AnthropicProvider(ModelProvider):
    """Resolve Anthropic chat models.

    Args:
        api_key_handle: Name of the environment variable holding the
            Anthropic API key. Defaults to ``ANTHROPIC_API_KEY``. When
            the env var is unset the SDK's own default resolution takes over.
    """

    def __init__(self, *, api_key_handle: str = "ANTHROPIC_API_KEY") -> None:
        self.api_key_handle = api_key_handle

    def build(self, spec: AgentSpec) -> BaseChatModel:
        """Return a configured :class:`ChatAnthropic` for ``spec``.

        The API key is read from ``os.environ[self.api_key_handle]``;
        when absent the SDK's own default resolution is used so tests
        that swap the client out via mocks do not require env setup.
        """
        from langchain_anthropic import ChatAnthropic

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
        return ChatAnthropic(**kwargs)

    def capabilities(self) -> ModelCapabilities:
        """Capabilities reflect Claude 3.5 Sonnet defaults (tool calling + vision, 200K ctx)."""
        return ModelCapabilities(
            tool_calling=True,
            streaming=True,
            thinking=False,
            vision=True,
            max_context_tokens=200_000,
        )


__all__ = ["AnthropicProvider"]
