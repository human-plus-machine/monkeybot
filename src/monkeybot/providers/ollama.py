"""Ollama provider via the OpenAI-compatible Chat Completions API.

Runs against a local Ollama server, a self-hosted host, or Ollama Cloud.
Ollama exposes an OpenAI-compatible endpoint at ``/v1`` on top of its native
API, so this adapter reuses the same request/response plumbing as
``OpenAIProvider`` and ``HuggingFaceProvider``.

YAML ``model.provider``:

- ``ollama-cloud`` — always ``https://ollama.com``. Ignores a leftover local
  ``OLLAMA_BASE_URL``. Requires ``OLLAMA_API_KEY``.
- ``ollama-local`` — always the local/self-hosted host. Ignores a cloud URL.
- ``ollama`` — legacy auto-route: a key with no URL means cloud; an explicit
  URL always wins (including ``http://127.0.0.1:11434``).

Configuration (environment variables or ``monkeybot.yaml``):

- ``OLLAMA_BASE_URL`` — OpenAI-compat base host. Used by ``ollama-local`` and
  legacy ``ollama``. Ignored by ``ollama-cloud`` unless it already points at
  ollama.com. ``/v1`` is appended when missing.
- ``OLLAMA_API_KEY`` — required for Ollama Cloud (ollama.com/settings/keys).
  Local Ollama ignores it; the OpenAI SDK still needs a non-empty string, so a
  dummy is used when unset.
- ``MODEL_NAME`` — model id passed to the API (e.g. ``llama3.1``, ``qwen2.5``,
  or a cloud id such as ``glm-5.3-flash``). Local models must already be pulled
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
from typing import Literal
from urllib.parse import urlparse

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

OllamaMode = Literal["auto", "cloud", "local"]

_DEFAULT_LOCAL_URL = "http://localhost:11434"
_DEFAULT_CLOUD_URL = "https://ollama.com"
_DUMMY_API_KEY = "ollama"
_log = logging.getLogger(__name__)


def _is_cloud_host(url: str) -> bool:
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    host = host.lower()
    return host == "ollama.com" or host.endswith(".ollama.com")


def _normalize_cloud_base(url: str) -> str:
    """Return a usable https URL for an Ollama Cloud host.

    Scheme-less values get ``https://``. Plaintext ``http://`` is upgraded so
    the API key never travels in the clear.
    """
    if "://" not in url:
        return f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme == "http":
        upgraded = parsed._replace(scheme="https").geturl()
        _log.warning(
            "ollama-cloud upgrading plaintext OLLAMA_BASE_URL %s",
            kv(ignored=url, host=upgraded),
        )
        return upgraded
    return url


def _resolve_host_and_key(mode: OllamaMode = "auto") -> tuple[str, str]:
    """Pick local vs cloud host from env and provider mode.

    ``cloud`` ignores a leftover localhost URL so a prior local-runtime persist
    cannot steal cloud traffic. ``local`` ignores a cloud URL and never forwards
    ``OLLAMA_API_KEY`` (an authenticating reverse proxy uses legacy ``ollama``
    plus an explicit URL). ``auto`` keeps the legacy rule: an explicit URL
    always wins.
    """
    api_key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    base = (os.environ.get("OLLAMA_BASE_URL") or "").strip().rstrip("/")
    if mode == "cloud":
        if not api_key:
            raise ValueError(
                "OLLAMA_API_KEY is not set. Create a key at "
                "https://ollama.com/settings/keys and add it to your .env or "
                "environment (required for model.provider ollama-cloud)."
            )
        if base and _is_cloud_host(base):
            return _normalize_cloud_base(base), api_key
        if base:
            _log.warning(
                "ollama-cloud ignoring non-cloud OLLAMA_BASE_URL %s",
                kv(ignored=base, host=_DEFAULT_CLOUD_URL),
            )
        return _DEFAULT_CLOUD_URL, api_key
    if mode == "local":
        if base and not _is_cloud_host(base):
            return base, _DUMMY_API_KEY
        if base:
            _log.warning(
                "ollama-local ignoring cloud OLLAMA_BASE_URL %s",
                kv(ignored=base, host=_DEFAULT_LOCAL_URL),
            )
        return _DEFAULT_LOCAL_URL, _DUMMY_API_KEY
    if base:
        return base, api_key or _DUMMY_API_KEY
    if api_key:
        _log.warning(
            "OLLAMA_API_KEY set with no OLLAMA_BASE_URL; using Ollama Cloud %s "
            "(set model.provider to ollama-local, or OLLAMA_BASE_URL, for a "
            "local reverse proxy)",
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
        if self._mode == "cloud":
            return "ollama-cloud"
        if self._mode == "local":
            return "ollama-local"
        return "ollama"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        mode: OllamaMode = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        self._mode = mode
        self._base_url, self._api_key = _resolve_host_and_key(mode)
        _log.info("ollama host resolved %s", kv(mode=self._mode, host=self._base_url))
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
        otherwise ``/v1`` is appended. Host selection is mode-driven
        (``ollama-cloud`` / ``ollama-local`` / legacy ``ollama``).
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
            provider=self.name,
            messages=messages,
            tools=tools,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            **effort_kw,
        ):
            yield event
