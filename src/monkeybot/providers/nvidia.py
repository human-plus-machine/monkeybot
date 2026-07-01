"""NVIDIA NIM provider via the OpenAI-compatible Chat Completions API.

Hosted catalog at https://build.nvidia.com/models — free personal API key,
~40 requests/minute. Same OpenAI-compat wire format as HuggingFace/Ollama.

Configuration (environment variables or ``monkeybot.yaml``):

- ``NVIDIA_API_KEY`` (**required**) — get one free at https://build.nvidia.com
  (open any model card → "Get API Key"); starts with ``nvapi-``
- ``NVIDIA_BASE_URL`` — OpenAI-compat base host (default:
  ``https://integrate.api.nvidia.com/v1``)
- ``MODEL_NAME`` — model id passed to the API (e.g. ``meta/llama-3.3-70b-instruct``)
- ``MODEL_TEMPERATURE`` — sampling temperature (default: ``0.7``; set via ``monkeybot.yaml`` / constructor)
- ``MODEL_MAX_TOKENS`` — max output tokens (default: ``60000``; set via ``monkeybot.yaml`` / constructor)

Install the required extra::

    uv sync --extra nvidia
    # or: pip install "monkeybot[nvidia]"
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

_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"


class NvidiaProvider:
    """NVIDIA-hosted models (build.nvidia.com) via the OpenAI-compatible endpoint.

    Requires ``monkeybot[nvidia]`` (``openai`` + ``tiktoken``) and ``NVIDIA_API_KEY``.
    """

    @property
    def name(self) -> str:
        return "nvidia"

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
        api_key = os.environ.get("NVIDIA_API_KEY", "")
        if not api_key:
            raise ValueError(
                "NVIDIA_API_KEY is not set. Get a free key at https://build.nvidia.com "
                "(open any model card and click 'Get API Key') and add it to your .env."
            )
        self._api_key = api_key
        self._base_url = (os.environ.get("NVIDIA_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        # ``cache_enabled`` is accepted for constructor-contract symmetry with the
        # other providers (Story 1) but is currently inert here: the OpenAI-compatible
        # request shape has no cache_control-equivalent field to set.
        self._cache_enabled = cache_enabled
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        return await count_input_tokens_tiktoken(
            messages, tools, model=model, thinking_budget=thinking_budget
        )

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
            base_url=self._base_url,
            api_key=self._api_key,
            provider="nvidia",
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        ):
            yield event
