"""Ollama provider via the OpenAI-compatible Chat Completions API.

Runs against a local Ollama server, a self-hosted host, or Ollama Cloud.
Ollama exposes an OpenAI-compatible endpoint at ``/v1`` on top of its native
API, so this adapter reuses the same request/response plumbing as
``OpenAIProvider`` and ``HuggingFaceProvider``.

Configuration (environment variables or ``monkeybot.yaml``):

- ``OLLAMA_BASE_URL`` — OpenAI-compat base host. Defaults to
  ``http://localhost:11434`` when no API key is set, or ``https://ollama.com``
  when ``OLLAMA_API_KEY`` is set and this is blank. ``/v1`` is appended when
  missing.
- ``OLLAMA_API_KEY`` — required for Ollama Cloud (ollama.com/settings/keys).
  Local Ollama ignores it; the OpenAI SDK still needs a non-empty string, so a
  dummy is used when unset. Set ``OLLAMA_BASE_URL`` explicitly if a local
  reverse proxy enforces auth.
- ``MODEL_NAME`` — model id passed to the API (e.g. ``llama3.1``, ``qwen2.5``,
  or a cloud id such as ``gpt-oss:120b``). Local models must already be pulled
  (``ollama pull <model>``).
- ``MODEL_TEMPERATURE`` — sampling temperature (default: ``0.7``; set via ``monkeybot.yaml`` / constructor)
- ``MODEL_MAX_TOKENS`` — max output tokens (default: ``60000``; set via ``monkeybot.yaml`` / constructor)
- ``MODEL_THINKING_BUDGET`` — thinking control for reasoning models (e.g. Gemma 4):
  ``-1`` = Ollama default (thinking on when supported), ``0`` = off via
  ``reasoning_effort: none``, ``N > 0`` = on (no token budget on Ollama).

Install the required extra::

    uv sync --extra ollama
    # or: pip install "monkeybot[ollama]"
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence

from monkeybot.core.llm.provider import (
    Message,
    ProviderEvent,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._openai_compat import (
    count_input_tokens_tiktoken,
    stream_chat_completions_with_tool_fallback,
)
from monkeybot.providers.sampling import resolve_model_sampling

_DEFAULT_LOCAL_URL = "http://localhost:11434"
_DEFAULT_CLOUD_URL = "https://ollama.com"
_DUMMY_API_KEY = "ollama"
_log = logging.getLogger(__name__)


def _resolve_host_and_key() -> tuple[str, str]:
    """Pick local vs cloud host from env.

    A key without a URL means Ollama Cloud. A URL always wins, so a reverse
    proxy in front of local Ollama can still authenticate by setting both.
    """
    api_key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    base = (os.environ.get("OLLAMA_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base, api_key or _DUMMY_API_KEY
    if api_key:
        _log.warning(
            "OLLAMA_API_KEY set with no OLLAMA_BASE_URL; using Ollama Cloud %s "
            "(set OLLAMA_BASE_URL explicitly for a local reverse proxy)",
            kv(host=_DEFAULT_CLOUD_URL),
        )
        return _DEFAULT_CLOUD_URL, api_key
    return _DEFAULT_LOCAL_URL, _DUMMY_API_KEY


def reasoning_effort_for_thinking_budget(budget: int) -> str | None:
    """Map monkeybot ``thinking_budget`` to Ollama OpenAI-compat ``reasoning_effort``.

    Returns ``None`` when the field should be omitted (server default).
    """
    if budget == 0:
        return "none"
    return None


def _resolve_thinking_budget(
    configured: int | None,
    *,
    override: int | None,
) -> int:
    if override is not None:
        return override
    if configured is not None:
        return configured
    return int(os.environ.get("MODEL_THINKING_BUDGET", "-1"))


class OllamaProvider:
    """Local, self-hosted, or Ollama Cloud models via the OpenAI-compatible endpoint.

    Requires ``monkeybot[ollama]`` (``openai`` + ``tiktoken``). Local use needs a
    reachable Ollama server with the model pulled; cloud use needs
    ``OLLAMA_API_KEY``.
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
        thinking_budget: int | None = None,
    ) -> None:
        self._base_url, self._api_key = _resolve_host_and_key()
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens
        self._thinking_budget = (
            thinking_budget
            if thinking_budget is not None
            else int(os.environ.get("MODEL_THINKING_BUDGET", "-1"))
        )

    def _resolve_base_url(self, model: str) -> str:
        """Return the OpenAI-compat base URL for ``model``.

        ``OLLAMA_BASE_URL`` is used as-is when it already ends in ``/v1``;
        otherwise ``/v1`` is appended. Defaults to local Ollama, or Ollama
        Cloud when ``OLLAMA_API_KEY`` is set and the URL is blank.
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
        budget = _resolve_thinking_budget(self._thinking_budget, override=thinking_budget)
        reasoning_effort = reasoning_effort_for_thinking_budget(budget)
        effort_kw = (
            {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
        )
        async for event in stream_chat_completions_with_tool_fallback(
            base_url=self._resolve_base_url(model),
            api_key=self._api_key,
            provider="ollama",
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            **effort_kw,
        ):
            yield event
