"""Exponential backoff retry for transient provider streaming failures."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, cast

from monkeybot.core.llm.provider import Message, Provider, ProviderEvent
from monkeybot.core.types.types_tools import ToolDef

_log = logging.getLogger(__name__)

_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_PERMANENT_HTTP_STATUSES = frozenset({400, 401, 403, 404, 413, 422})

_DEFAULT_MAX_ATTEMPTS = 4
_DEFAULT_BASE_DELAY_S = 1.0
_DEFAULT_MAX_DELAY_S = 60.0
_DEFAULT_JITTER_FRACTION = 0.25


@dataclass(frozen=True)
class ProviderRetryConfig:
    """Retry policy for one provider stream request."""

    enabled: bool = True
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    base_delay_s: float = _DEFAULT_BASE_DELAY_S
    max_delay_s: float = _DEFAULT_MAX_DELAY_S
    jitter_fraction: float = _DEFAULT_JITTER_FRACTION


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def provider_retry_config_from_env() -> ProviderRetryConfig:
    """Load retry settings from ``MONKEYBOT_PROVIDER_RETRY_*`` env vars."""
    return ProviderRetryConfig(
        enabled=_env_bool("MONKEYBOT_PROVIDER_RETRY_ENABLED", True),
        max_attempts=max(1, _env_int("MONKEYBOT_PROVIDER_RETRY_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)),
        base_delay_s=max(0.0, _env_float("MONKEYBOT_PROVIDER_RETRY_BASE_DELAY_SEC", _DEFAULT_BASE_DELAY_S)),
        max_delay_s=max(
            0.0,
            _env_float("MONKEYBOT_PROVIDER_RETRY_MAX_DELAY_SEC", _DEFAULT_MAX_DELAY_S),
        ),
        jitter_fraction=min(
            1.0,
            max(0.0, _env_float("MONKEYBOT_PROVIDER_RETRY_JITTER_FRACTION", _DEFAULT_JITTER_FRACTION)),
        ),
    )


def _walk_exceptions(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _http_status_code(exc: BaseException) -> int | None:
    for item in _walk_exceptions(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(item, "response", None)
        if response is not None:
            response_status = getattr(response, "status_code", None)
            if isinstance(response_status, int):
                return response_status
    return None


def _retry_after_header_seconds(exc: BaseException) -> float | None:
    for item in _walk_exceptions(exc):
        response = getattr(item, "response", None)
        if response is None:
            continue
        headers = getattr(response, "headers", None)
        if headers is None:
            continue
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            continue
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _message_suggests_transient(exc: BaseException) -> bool:
    msg = " ".join(str(item) for item in _walk_exceptions(exc)).lower()
    transient_markers = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "resource exhausted",
        "resource_exhausted",
        "overloaded",
        "temporarily unavailable",
        "service unavailable",
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "server error",
        "bad gateway",
        "gateway timeout",
    )
    return any(marker in msg for marker in transient_markers)


def _is_sdk_transient(exc: BaseException) -> bool:
    for item in _walk_exceptions(exc):
        module = type(item).__module__
        name = type(item).__name__
        if module.startswith("anthropic") and name in {
            "RateLimitError",
            "InternalServerError",
            "APIConnectionError",
            "APITimeoutError",
        }:
            return True
        if module.startswith("openai") and name in {
            "RateLimitError",
            "InternalServerError",
            "APIConnectionError",
            "APITimeoutError",
        }:
            return True
        if module.startswith("google.api_core") and name in {
            "TooManyRequests",
            "ServiceUnavailable",
            "InternalServerError",
            "GatewayTimeout",
            "DeadlineExceeded",
            "Aborted",
        }:
            return True
        if module.startswith("httpx") and name in {
            "ConnectError",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "NetworkError",
        }:
            return True
    return False


def is_transient_provider_error(exc: BaseException) -> bool:
    """Return True when ``exc`` looks like a short-lived provider/network failure."""
    if isinstance(exc, asyncio.CancelledError):
        return False

    status = _http_status_code(exc)
    if status is not None:
        if status in _PERMANENT_HTTP_STATUSES:
            return False
        if status in _TRANSIENT_HTTP_STATUSES:
            return True

    if _is_sdk_transient(exc):
        return True

    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    return _message_suggests_transient(exc)


def compute_retry_delay_seconds(
    attempt: int,
    exc: BaseException,
    config: ProviderRetryConfig,
) -> float:
    """Backoff delay before retry attempt ``attempt`` (0-based)."""
    retry_after = _retry_after_header_seconds(exc)
    base = retry_after if retry_after is not None else config.base_delay_s * (2**attempt)

    delay = min(base, config.max_delay_s)
    if config.jitter_fraction > 0:
        jitter = delay * config.jitter_fraction * random.random()
        delay += jitter
    return delay


async def retrying_provider_stream(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    model: str,
    thinking_budget: int | None = None,
    config: ProviderRetryConfig | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Stream provider output with bounded exponential backoff on transient failures.

    Retries only when the stream fails **before** the first event is yielded, so
    partial assistant/tool output is never duplicated.
    """
    cfg = config or provider_retry_config_from_env()
    if not cfg.enabled or cfg.max_attempts <= 1:
        async for event in provider.stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
        ):
            yield event
        return

    last_exc: BaseException | None = None
    for attempt in range(cfg.max_attempts):
        emitted = False
        try:
            async with aclosing(
                cast(
                    Any,
                    provider.stream(
                        messages,
                        tools,
                        model=model,
                        thinking_budget=thinking_budget,
                    ),
                )
            ) as stream:
                async for event in stream:
                    emitted = True
                    yield event
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            is_last_attempt = attempt + 1 >= cfg.max_attempts
            if emitted or not is_transient_provider_error(exc) or is_last_attempt:
                raise
            delay = compute_retry_delay_seconds(attempt, exc, cfg)
            _log.warning(
                "provider stream transient error (attempt %d/%d, provider=%s, model=%s); "
                "retrying in %.2fs: %s",
                attempt + 1,
                cfg.max_attempts,
                provider.name,
                model,
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc


__all__ = [
    "ProviderRetryConfig",
    "compute_retry_delay_seconds",
    "is_transient_provider_error",
    "provider_retry_config_from_env",
    "retrying_provider_stream",
]
