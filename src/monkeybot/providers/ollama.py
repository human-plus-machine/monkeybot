"""Ollama provider via the OpenAI-compatible Chat Completions API.

Runs against a local (or remote) Ollama server — no API key required. Ollama
exposes an OpenAI-compatible endpoint at ``/v1`` on top of its native API, so
this adapter reuses the same request/response plumbing as ``OpenAIProvider``
and ``HuggingFaceProvider``.

Configuration (environment variables or ``monkeybot.yaml``):

- ``OLLAMA_BASE_URL`` — OpenAI-compat base host (default: ``http://localhost:11434``;
  ``/v1`` is appended when missing)
- ``OLLAMA_API_KEY`` — optional; Ollama ignores the value but the OpenAI SDK
  requires a non-empty string. Set this only if a reverse proxy enforces auth.
- ``MODEL_NAME`` — model id passed to the API (e.g. ``llama3.1``, ``qwen2.5``).
  Must already be pulled on the Ollama server (``ollama pull <model>``).
- ``MODEL_TEMPERATURE`` — sampling temperature (default: ``0.7``; set via ``monkeybot.yaml`` / constructor)
- ``MODEL_MAX_TOKENS`` — max output tokens (default: ``60000``; set via ``monkeybot.yaml`` / constructor)

Install the required extra::

    uv sync --extra ollama
    # or: pip install "monkeybot[ollama]"
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence

from monkeybot.core.llm.provider import (
    Message,
    ProviderEvent,
)
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._openai_compat import (
    count_input_tokens_tiktoken,
    stream_chat_completions_with_tool_fallback,
)
from monkeybot.providers.sampling import resolve_model_sampling

_DEFAULT_BASE_URL = "http://localhost:11434"
_DUMMY_API_KEY = "ollama"


class OllamaProvider:
    """Local (or self-hosted) models via Ollama's OpenAI-compatible endpoint.

    Requires ``monkeybot[ollama]`` (``openai`` + ``tiktoken``) and a reachable
    Ollama server with the configured model already pulled. No API key is
    required by default.
    """

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self._base_url = (os.environ.get("OLLAMA_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self._api_key = os.environ.get("OLLAMA_API_KEY") or _DUMMY_API_KEY
        # ``cache_enabled`` is accepted for constructor-contract symmetry with the
        # other providers (Story 1) but is currently inert here: the OpenAI-compatible
        # request shape has no cache_control-equivalent field to set.
        self._cache_enabled = cache_enabled
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

    def _resolve_base_url(self, model: str) -> str:
        """Return the OpenAI-compat base URL for ``model``.

        ``OLLAMA_BASE_URL`` is used as-is when it already ends in ``/v1``;
        otherwise ``/v1`` is appended. Defaults to a local Ollama server.
        """
        del model  # model id is passed per request; base URL is env-driven
        host = self._base_url
        if host.endswith("/v1"):
            return host
        return f"{host}/v1"

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> int:
        return await count_input_tokens_tiktoken(messages, tools, model=model)

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del thinking_budget
        async for event in stream_chat_completions_with_tool_fallback(
            base_url=self._resolve_base_url(model),
            api_key=self._api_key,
            provider="ollama",
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        ):
            yield event
