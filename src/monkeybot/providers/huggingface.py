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
- ``MODEL_TEMPERATURE`` — sampling temperature (default: ``0.7``)
- ``MODEL_MAX_TOKENS`` — max output tokens (default: ``4096``)

Install the required extra::

    uv sync --extra huggingface
    # or: pip install "monkeybot[huggingface]"
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._openai_compat import (
    iter_openai_compat_stream,
    messages_to_openai,
    openai_messages_token_count,
    openai_tools,
    openai_tools_token_count,
)

_log = logging.getLogger(__name__)

_DEFAULT_HOST = "https://router.huggingface.co/hf-inference"

# Keep these names importable to silence unused-import linters
_ = (Done, TextDelta, ToolCall, UsageEvent)


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

    def __init__(self, *, cache_enabled: bool = True) -> None:
        token = os.environ.get("HF_TOKEN", "")
        if not token:
            raise ValueError(
                "HF_TOKEN is not set. Create a token at https://huggingface.co/settings/tokens "
                "and add it to your .env or environment."
            )
        self._token = token
        self._endpoint_url = (os.environ.get("HF_ENDPOINT_URL") or "").rstrip("/")
        self._host = (os.environ.get("HF_BASE_URL") or _DEFAULT_HOST).rstrip("/")
        self._cache_enabled = cache_enabled

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
    ) -> int:
        import tiktoken  # noqa: PLC0415

        msgs = list(messages)
        system, oai_messages = messages_to_openai(msgs)
        if system:
            oai_messages = [{"role": "system", "content": system}, *oai_messages]
        tool_defs = openai_tools(tools) if tools else []
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return openai_messages_token_count(enc, oai_messages) + openai_tools_token_count(enc, tool_defs)

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        from openai import AsyncOpenAI  # noqa: PLC0415

        msgs = list(messages)
        temperature = float(os.environ.get("MODEL_TEMPERATURE", "0.7"))
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", "4096"))

        system, oai_messages = messages_to_openai(msgs)
        if system:
            oai_messages = [{"role": "system", "content": system}, *oai_messages]

        client = AsyncOpenAI(base_url=self._resolve_base_url(model), api_key=self._token)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = openai_tools(tools)

        try:
            async for event in iter_openai_compat_stream(client, kwargs):
                yield event
        except Exception as exc:
            if tools and _is_tool_unsupported_error(exc):
                _log.warning(
                    "HuggingFace model %r does not support tool calling; retrying without tools. "
                    "Error: %s",
                    model,
                    exc,
                )
                kwargs.pop("tools", None)
                async for event in iter_openai_compat_stream(client, kwargs):
                    yield event
            else:
                raise


def _is_tool_unsupported_error(exc: BaseException) -> bool:
    """Return True when the exception likely indicates unsupported tool calling."""
    try:
        from openai import APIStatusError  # noqa: PLC0415
    except ImportError:
        APIStatusError = None  # type: ignore[misc, assignment]

    if APIStatusError is not None and isinstance(exc, APIStatusError):
        if exc.status_code not in (400, 422):
            return False
        body = str(exc.body or exc.message or "").lower()
        return any(
            phrase in body
            for phrase in (
                "tool",
                "function_call",
                "functions",
                "unsupported",
            )
        )

    msg = str(exc).lower()
    return "tool" in msg and "unsupported" in msg
