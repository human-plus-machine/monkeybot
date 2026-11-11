"""Decide whether a failed model call is retried, resumed, or given up.

A call is safe to send again when it failed for a transient reason and no
assistant text from it reached the client. Text the client already has cannot
be withdrawn, so that attempt is kept and the next inner turn continues it.
Anything else ends the turn the way a provider error always has.
"""

from __future__ import annotations

import importlib
import logging
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from monkeybot.core.config.snapshot import env_value_or_current
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.preempt import StreamStalledError

_log = logging.getLogger("monkeybot.core.runtime.provider_retry")

FailureKind = Literal["rate_limit", "server", "connection", "stall", "fatal"]
Recovery = Literal["retry", "resume", "give_up"]

# Statuses that mean "try the same request again". 429 is a rate limit, 5xx
# (including Anthropic's 529 overloaded) is a server failure, and the rest are
# connection-shaped even when the HTTP status is a 4xx.
_RATE_LIMIT_STATUS = 429
_CONNECTION_STATUS = frozenset({408, 409, 425})
_RETRY_AFTER_CAP_S = 60.0
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_IDLE_TIMEOUT_S = 300.0

RESUME_NOTICE = "The response was cut off. Continuing."
RESUME_NOTE = (
    "[Harness] The previous response was cut off before it finished. "
    "Continue from where it stopped without repeating text the user already saw."
)

_KIND_LABEL = {
    "rate_limit": "Provider rate-limited",
    "server": "Provider error",
    "connection": "Connection lost",
    "stall": "Provider stalled",
}

# Tests install a zero-delay policy here. Production leaves it unset and reads
# model.provider_max_retries. A module global, not a context var: a test that
# drives the loop through asyncio.run must see the same policy.
_policy_override: RetryPolicy | None = None
_IDLE_UNSET = object()
_idle_override: object = _IDLE_UNSET
_sdk_connection_types: tuple[type[BaseException], ...] | None = None


@dataclass(frozen=True, kw_only=True)
class Failure:
    """What kind of provider failure this exception is.

    ``retry_after_s`` is the server's requested wait, uncapped. The policy
    caps it when it chooses a delay.
    """

    kind: FailureKind
    retry_after_s: float | None = None

    @property
    def transient(self) -> bool:
        return self.kind != "fatal"


@dataclass(frozen=True, kw_only=True)
class RetryPolicy:
    """How many times one inner call may be sent again, and how long to wait.

    ``max_retries`` counts extra attempts after the first. Zero disables every
    loop retry, including stalls and resumes. Stalls spend ``stall_retries``
    because each one already waited out the idle timeout. Resumes, where the
    client saw text, are capped separately for the whole user request.
    """

    max_retries: int = _DEFAULT_MAX_RETRIES
    stall_retries: int = 1
    max_resumes: int = 2
    base_delays_s: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)
    jitter_s: float = 0.5
    retry_after_cap_s: float = _RETRY_AFTER_CAP_S
    honor_retry_after: bool = True

    def delay_s(self, *, attempt: int, failure: Failure) -> float:
        """Seconds to wait before extra attempt ``attempt`` (1-based)."""
        if (
            self.honor_retry_after
            and failure.retry_after_s is not None
            and failure.retry_after_s >= 0
        ):
            base = min(failure.retry_after_s, self.retry_after_cap_s)
        else:
            index = min(max(attempt, 1) - 1, len(self.base_delays_s) - 1)
            base = self.base_delays_s[index]
        if self.jitter_s <= 0:
            return base
        return base + random.uniform(0, self.jitter_s)


def testing_policy() -> RetryPolicy:
    """The suite's policy: real retry counts, no sleeping."""
    return RetryPolicy(
        max_retries=_DEFAULT_MAX_RETRIES,
        stall_retries=1,
        max_resumes=2,
        base_delays_s=(0.0, 0.0, 0.0, 0.0, 0.0),
        jitter_s=0.0,
        honor_retry_after=False,
    )


@contextmanager
def use_retry_policy(policy: RetryPolicy | None) -> Iterator[None]:
    """Install ``policy`` for the current test. ``None`` reads configuration."""
    global _policy_override
    previous = _policy_override
    _policy_override = policy
    try:
        yield
    finally:
        _policy_override = previous


@contextmanager
def use_idle_timeout(seconds: float | None) -> Iterator[None]:
    """Install an idle timeout for the current test. ``None`` disables it."""
    global _idle_override
    previous = _idle_override
    _idle_override = seconds
    try:
        yield
    finally:
        _idle_override = previous


def policy_for(cfg: Any) -> RetryPolicy:
    """The active policy. A test override wins; otherwise the snapshot does."""
    if _policy_override is not None:
        return _policy_override
    return RetryPolicy(max_retries=_configured_max_retries(cfg))


def idle_timeout_s(cfg: Any) -> float | None:
    """Seconds without a stream event before the call is stalled.

    ``None`` disables the timeout. Zero and negative configuration values
    disable it too.
    """
    if _idle_override is not _IDLE_UNSET:
        value = None if _idle_override is None else float(_idle_override)  # type: ignore[arg-type]
    else:
        raw = env_value_or_current(cfg, "STREAM_IDLE_TIMEOUT_S", str(int(_DEFAULT_IDLE_TIMEOUT_S)))
        value = _parse_idle(raw)
    if value is None or value <= 0:
        return None
    return value


def recovery(
    failure: Failure,
    policy: RetryPolicy,
    *,
    retries_used: int,
    stall_retries_used: int,
    resumes_used: int,
    text_reached_client: bool,
) -> Recovery:
    """Choose what happens to this failed attempt.

    Text the client already saw is resumed, not replayed. A stall spends the
    stall budget; every other transient failure spends the retry budget.
    """
    if not failure.transient or policy.max_retries <= 0:
        return "give_up"
    if text_reached_client:
        if resumes_used >= policy.max_resumes:
            return "give_up"
        return "resume"
    if failure.kind == "stall":
        if stall_retries_used >= policy.stall_retries:
            return "give_up"
        return "retry"
    if retries_used >= policy.max_retries:
        return "give_up"
    return "retry"


def retry_notice(failure: Failure, *, attempt: int, limit: int, delay_s: float) -> str:
    """One line for the client while the same request is sent again."""
    label = _KIND_LABEL[failure.kind]
    if delay_s < 0.05:
        when = "now"
    elif delay_s < 10:
        when = f"in {delay_s:.1f} s"
    else:
        when = f"in {delay_s:.0f} s"
    return f"{label}. Retrying {when} ({attempt} of {limit})"


def classify(exc: BaseException) -> Failure:
    """Classify ``exc``, walking ``__cause__``, ``__context__``, and ``.original``.

    The first matching kind wins, in the order stall, rate limit, server,
    connection. An exception this function does not recognise is fatal, so a
    bug fails once instead of being sent again.
    """
    chain = related_exceptions(exc)
    retry_after = _retry_after_s(chain)
    if any(isinstance(item, StreamStalledError) for item in chain):
        return Failure(kind="stall", retry_after_s=retry_after)
    if any(_is_rate_limit(item) for item in chain):
        return Failure(kind="rate_limit", retry_after_s=retry_after)
    if any(_is_server(item) for item in chain):
        return Failure(kind="server", retry_after_s=retry_after)
    if any(_is_connection(item) for item in chain):
        return Failure(kind="connection", retry_after_s=retry_after)
    return Failure(kind="fatal")


def _configured_max_retries(cfg: Any) -> int:
    raw = env_value_or_current(cfg, "PROVIDER_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES)).strip()
    if not raw:
        return _DEFAULT_MAX_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        _log.warning(
            "invalid PROVIDER_MAX_RETRIES %s; using %s",
            kv(value=raw),
            _DEFAULT_MAX_RETRIES,
        )
        return _DEFAULT_MAX_RETRIES


def _parse_idle(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return _DEFAULT_IDLE_TIMEOUT_S
    try:
        return float(text)
    except ValueError:
        _log.warning(
            "invalid STREAM_IDLE_TIMEOUT_S %s; using %s",
            kv(value=text),
            int(_DEFAULT_IDLE_TIMEOUT_S),
        )
        return _DEFAULT_IDLE_TIMEOUT_S


def related_exceptions(exc: BaseException) -> tuple[BaseException, ...]:
    """``exc`` and the exceptions it wraps, outermost first."""
    seen: set[int] = set()
    ordered: list[BaseException] = []
    stack = [exc]
    while stack:
        current = stack.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        ordered.append(current)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        context = current.__context__
        if (
            context is not None
            and context is not current.__cause__
            and not current.__suppress_context__
        ):
            stack.append(context)
        original = getattr(current, "original", None)
        if isinstance(original, BaseException):
            stack.append(original)
    return tuple(ordered)


def status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _is_rate_limit(exc: BaseException) -> bool:
    try:
        from monkeybot.providers._openai_compat import (  # noqa: PLC0415
            ProviderRateLimitError,
            is_rate_limit_error,
        )
    except ImportError:  # pragma: no cover - openai compat is part of the package
        return status_code(exc) == _RATE_LIMIT_STATUS
    if isinstance(exc, ProviderRateLimitError):
        return True
    if is_rate_limit_error(exc):
        return True
    return status_code(exc) == _RATE_LIMIT_STATUS


def _is_server(exc: BaseException) -> bool:
    status = status_code(exc)
    if status is not None and status >= 500:
        return True
    try:
        from monkeybot.providers._openai_compat import (  # noqa: PLC0415
            ProviderServerError,
            is_server_error,
        )
    except ImportError:  # pragma: no cover - openai compat is part of the package
        return False
    if isinstance(exc, ProviderServerError):
        return True
    return is_server_error(exc)


def _is_connection(exc: BaseException) -> bool:
    if isinstance(exc, StreamStalledError):
        return False
    status = status_code(exc)
    if status in _CONNECTION_STATUS:
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return isinstance(exc, _load_sdk_connection_types())


def _load_sdk_connection_types() -> tuple[type[BaseException], ...]:
    global _sdk_connection_types
    if _sdk_connection_types is not None:
        return _sdk_connection_types
    found: list[type[BaseException]] = []
    for module_name, names in (
        ("openai", ("APIConnectionError", "APITimeoutError")),
        ("anthropic", ("APIConnectionError", "APITimeoutError")),
        ("httpx", ("TimeoutException", "NetworkError")),
        ("aiohttp", ("ServerTimeoutError", "ClientConnectionError", "ClientPayloadError")),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in names:
            candidate = getattr(module, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                found.append(candidate)
    _sdk_connection_types = tuple(found)
    return _sdk_connection_types


def _retry_after_s(chain: tuple[BaseException, ...]) -> float | None:
    for exc in chain:
        parsed = _retry_after_header(exc)
        if parsed is not None:
            return parsed
    return None


def _retry_after_header(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    milliseconds = getter("retry-after-ms")
    if milliseconds is not None:
        try:
            return max(0.0, float(str(milliseconds).strip()) / 1000.0)
        except ValueError:
            pass
    raw = getter("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
