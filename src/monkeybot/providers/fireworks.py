"""Fireworks AI provider via the OpenAI-compatible Chat Completions API.

Serverless open-weight models — see https://fireworks.ai/models.
Same OpenAI-compat wire format as NVIDIA/HuggingFace/Ollama.

Configuration (environment variables or ``monkeybot.yaml``):

- ``FIREWORKS_API_KEY`` (**required**) — get one at
  https://fireworks.ai/account/api-keys
- ``FIREWORKS_BASE_URL`` — OpenAI-compat base host (default:
  ``https://api.fireworks.ai/inference/v1``)
- ``model.name`` — model id passed to the API (e.g.
  ``accounts/fireworks/models/llama-v3p3-70b-instruct``)
- ``model.temperature`` — sampling temperature (default: ``0.7``; YAML-only)
- ``model.max_tokens`` — max output tokens (default: ``60000``; YAML-only)

Install the required extra::

    uv sync --extra fireworks
    # or: pip install "monkeybot[fireworks]"
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
from monkeybot.providers.request_budget import configured_request_byte_budget
from monkeybot.providers.sampling import resolve_model_sampling

_DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"


class FireworksProvider:
    """Fireworks-hosted models via the OpenAI-compatible endpoint.

    Requires ``monkeybot[fireworks]`` (``openai`` + ``tiktoken``) and ``FIREWORKS_API_KEY``.
    """

    @property
    def name(self) -> str:
        return "fireworks"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        api_key = os.environ.get("FIREWORKS_API_KEY", "")
        if not api_key:
            raise ValueError(
                "FIREWORKS_API_KEY is not set. Create a key at https://fireworks.ai/account/api-keys "
                "and add it to your .env."
            )
        self._api_key = api_key
        self._base_url = (os.environ.get("FIREWORKS_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
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
        # ponytail: tiktoken estimate, models here aren't GPT — swap for the
        # usage echo on /chat/completions if the drift matters.
        return await count_input_tokens_tiktoken(
            messages,
            tools,
            model=model,
            provider=self.name,
            max_request_bytes=configured_request_byte_budget(provider=self.name),
            thinking_budget=thinking_budget,
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
            provider="fireworks",
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            max_request_bytes=configured_request_byte_budget(provider=self.name),
        ):
            yield event
