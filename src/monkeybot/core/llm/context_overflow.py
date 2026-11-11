"""Decide whether a provider failure means the request was too large.

Only a rejection is evidence. The harness's own token estimate is not.
A rate limit never matches, even when its text mentions tokens.
"""

from __future__ import annotations

# Substrings, compared in lower case. Each one is a phrase a provider uses
# when it refuses a request for size. A 400, a 413, or an error with no status
# matches only when its text contains one of these.
OVERFLOW_MESSAGE_PATTERNS: tuple[str, ...] = (
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "prompt is too long",
    "input is too long",
    "exceeds the maximum number of tokens",
    "input token count exceeds",
    "too many tokens",
)

_OVERFLOW_CODE = "context_length_exceeded"
_SIZE_STATUSES = frozenset({400, 413})
_RATE_LIMIT_STATUS = 429


def is_context_overflow(exc: BaseException) -> bool:
    """True when ``exc`` or an exception it wraps is a size rejection."""
    return any(_is_overflow(item) for item in _related(exc))


def _is_overflow(exc: BaseException) -> bool:
    if _status_code(exc) == _RATE_LIMIT_STATUS:
        return False
    if _is_byte_budget(exc):
        return True
    if _error_code(exc) == _OVERFLOW_CODE:
        return True
    status = _status_code(exc)
    if status is not None and status not in _SIZE_STATUSES:
        return False
    text = _text(exc)
    return any(pattern in text for pattern in OVERFLOW_MESSAGE_PATTERNS)


def _is_byte_budget(exc: BaseException) -> bool:
    try:
        from monkeybot.providers.request_budget import RequestByteBudgetError
    except ImportError:  # pragma: no cover - request_budget is part of the package
        return False
    return isinstance(exc, RequestByteBudgetError)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            nested = error.get("code")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _status_code(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        if obj is None:
            continue
        for attr in ("status_code", "code"):
            value = getattr(obj, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _text(exc: BaseException) -> str:
    parts = [str(exc)]
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        parts.append(message)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            parts.append(error["message"])
        elif isinstance(error, str):
            parts.append(error)
    return " ".join(parts).lower()


def _related(exc: BaseException) -> tuple[BaseException, ...]:
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
