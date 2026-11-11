"""Text safe to put on a client Error frame.

Provider SDKs stringify to the response body. That body can carry request
payloads and account details, so the client gets a fixed sentence for the
kind of failure, plus the HTTP status and the request id when those are
known. The raw exception stays in the server log.
"""

from __future__ import annotations

from monkeybot.core.llm.context_overflow import is_context_overflow
from monkeybot.core.runtime.provider_retry import classify, related_exceptions, status_code

_KIND_MESSAGE = {
    "stall": "The model provider stopped responding.",
    "rate_limit": "The model provider is rate-limiting requests. Wait a moment and try again.",
    "server": "The model provider returned a server error. Try again in a moment.",
    "connection": "Lost the connection to the model provider.",
}
_CREDENTIALS = "The model provider rejected the credentials. Check the API key."
_REJECTED = "The model provider rejected the request."
_AUTH_STATUSES = frozenset({401, 403})


def client_error_text(exc: BaseException, *, request_id: str) -> str:
    """One sentence for the client. Never the provider's response body."""
    facing = _user_facing_message(exc)
    if facing is not None:
        return facing
    if is_context_overflow(exc):
        # Imported here: turn_loop is the owner of the sentence, and it imports
        # this module for the Error frame.
        from monkeybot.core.runtime.turn_loop import CONTEXT_OVERFLOW_ERROR

        return CONTEXT_OVERFLOW_ERROR
    failure = classify(exc)
    message = _KIND_MESSAGE.get(failure.kind)
    status = _first_status(exc)
    if message is None:
        if status in _AUTH_STATUSES:
            message = _CREDENTIALS
        elif status is not None and 400 <= status < 500:
            message = _REJECTED
        else:
            message = f"The request failed ({type(exc).__name__})."
    return message + _suffix(status, request_id)


def _user_facing_message(exc: BaseException) -> str | None:
    """The outermost exception that already wrote its own client sentence."""
    for item in related_exceptions(exc):
        if getattr(item, "user_facing", False) is True:
            return str(item)
    return None


def _first_status(exc: BaseException) -> int | None:
    for item in related_exceptions(exc):
        status = status_code(item)
        if status is not None:
            return status
    return None


def _suffix(status: int | None, request_id: str) -> str:
    parts: list[str] = []
    if status is not None:
        parts.append(f"HTTP {status}")
    ref = request_id.strip()
    if ref:
        parts.append(f"ref {ref}")
    if not parts:
        return ""
    return f" ({', '.join(parts)})"
