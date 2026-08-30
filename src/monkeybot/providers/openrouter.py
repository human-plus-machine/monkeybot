"""OpenRouter provider via the OpenAI-compatible Chat Completions API.

Routes to hundreds of models from one key — see https://openrouter.ai/models.
Same OpenAI-compat wire format as NVIDIA/HuggingFace/Ollama.

Configuration (environment variables or ``monkeybot.yaml``):

- ``OPENROUTER_API_KEY`` (**required**) — get one at https://openrouter.ai/keys;
  starts with ``sk-or-``
- ``OPENROUTER_BASE_URL`` — OpenAI-compat base host (default:
  ``https://openrouter.ai/api/v1``)
- ``MODEL_NAME`` — model id passed to the API (e.g. ``anthropic/claude-sonnet-4.5``)
- ``MODEL_TEMPERATURE`` — sampling temperature (default: ``0.7``; set via ``monkeybot.yaml`` / constructor)
- ``MODEL_MAX_TOKENS`` — max output tokens (default: ``60000``; set via ``monkeybot.yaml`` / constructor)

Install the required extra::

    uv sync --extra openrouter
    # or: pip install "monkeybot[openrouter]"
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

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """OpenRouter-hosted models via the OpenAI-compatible endpoint.

    Requires ``monkeybot[openrouter]`` (``openai`` + ``tiktoken``) and ``OPENROUTER_API_KEY``.
    """

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def supports_tool_result_media(self) -> bool:
        return False

    def __init__(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is not set. Create a key at https://openrouter.ai/keys "
                "and add it to your .env."
            )
        self._api_key = api_key
        self._base_url = (os.environ.get("OPENROUTER_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
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
        # ponytail: tiktoken estimate, models here aren't all GPT — swap for
        # OpenRouter's /api/v1/chat/completions usage echo if the drift matters.
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
            provider="openrouter",
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        ):
            yield event
