"""Hugging Face provider via the OpenAI-compatible Chat Completions API.

Default host is the HF inference router (``router.huggingface.co/hf-inference``).
Override with ``HF_BASE_URL`` for provider-specific routers (e.g. Cerebras) or
``HF_ENDPOINT_URL`` for a dedicated Inference Endpoint.

Configuration (environment variables or ``monkeybot.yaml``):

- ``HF_TOKEN`` (**required**) — Hugging Face API token
- ``HF_ENDPOINT_URL`` — full base URL of a dedicated Inference Endpoint
  (takes precedence over ``HF_BASE_URL`` and the default)
- ``HF_BASE_URL`` — OpenAI-compat base host (default:
  ``https://router.huggingface.co/hf-inference``; append ``/v1`` when missing)
- ``MODEL_NAME`` — model id passed to the API (e.g. ``meta-llama/Llama-3.1-8B-Instruct``)
- ``MODEL_TEMPERATURE`` — sampling temperature (default: ``0.7``; set via ``monkeybot.yaml`` / constructor)
- ``MODEL_MAX_TOKENS`` — max output tokens (default: ``60000``; set via ``monkeybot.yaml`` / constructor)

Install the required extra::

    uv sync --extra huggingface
    # or: pip install "monkeybot[huggingface]"
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
    is_tool_unsupported_error,
    stream_chat_completions_with_tool_fallback,
)
from monkeybot.providers.sampling import resolve_model_sampling

_DEFAULT_HOST = "https://router.huggingface.co/hf-inference"

# Re-exported for tests that historically imported this private name from here.
_is_tool_unsupported_error = is_tool_unsupported_error


class HuggingFaceProvider:
    """Hugging Face models using the OpenAI-compatible Chat Completions endpoint.

    Requires ``monkeybot[huggingface]`` (``openai`` + ``tiktoken``) and ``HF_TOKEN``.
    """

    @property
    def name(self) -> str:
        return "huggingface"

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
        token = os.environ.get("HF_TOKEN", "")
        if not token:
            raise ValueError(
                "HF_TOKEN is not set. Create a token at https://huggingface.co/settings/tokens "
                "and add it to your .env or environment."
            )
        self._token = token
        self._endpoint_url = (os.environ.get("HF_ENDPOINT_URL") or "").rstrip("/")
        self._host = (os.environ.get("HF_BASE_URL") or _DEFAULT_HOST).rstrip("/")
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

    def _resolve_base_url(self, model: str) -> str:
        """Return the OpenAI-compat base URL for ``model``.

        URL resolution priority:
        1. ``HF_ENDPOINT_URL`` — dedicated Inference Endpoint, used as-is
        2. ``HF_BASE_URL`` — when it already ends in ``/v1``, used as-is; else ``/v1`` is appended
        3. Default — ``router.huggingface.co/hf-inference/v1``

        Router examples: ``HF_BASE_URL=https://router.huggingface.co/cerebras/v1``.
        """
        del model  # model id is passed per request; endpoint URL is env-driven
        if self._endpoint_url:
            return self._endpoint_url
        host = self._host
        if host.endswith("/v1"):
            return host
        return f"{host}/v1"

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
            base_url=self._resolve_base_url(model),
            api_key=self._token,
            provider="huggingface",
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        ):
            yield event
