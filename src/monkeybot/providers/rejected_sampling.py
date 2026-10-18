"""Remember sampling fields a model rejected so the next turn does not resend them."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Mapping

from monkeybot.core.logging_utils import kv

_log = logging.getLogger(__name__)

_ERROR_CODE_400 = re.compile(r"error code:\s*400\b", re.IGNORECASE)
# google-genai formats errors as ``400 INVALID_ARGUMENT. {...}``.
_LEADING_400 = re.compile(r"^\s*400\s+[A-Z_]+\b")

# Matches any value. Used when a caller records a name without the value it sent.
_ANY = object()

# (provider, model) -> param -> rejected values. Keyed on the value so a changed
# config value (e.g. a new reasoning_effort after a reload) is tried again.
_rejected: dict[tuple[str, str], dict[str, list[object]]] = {}
_lock = threading.Lock()


def is_status_400(exc: BaseException) -> bool:
    """True for an HTTP 400.

    A numeric ``status_code`` (OpenAI / Anthropic) or ``code`` (google-genai) wins.
    Otherwise only the SDK's ``Error code: 400`` or genai's ``400 STATUS`` prefix
    counts, so a limit like ``max_tokens 4000`` is not treated as a 400.
    """
    for attr in ("status_code", "code"):
        status = getattr(exc, attr, None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status == 400
    text = str(exc)
    return _ERROR_CODE_400.search(text) is not None or _LEADING_400.match(text) is not None


def blames_field_path(msg: str, param: str) -> bool:
    """True if ``msg`` names ``param`` as a field path (``temperature: ...``).

    A bare mention of the word does not count.
    """
    return (
        re.search(rf"(?:^|[^a-z0-9_.]){re.escape(param)}(?:\.[a-z0-9_.]+)?:", msg.lower())
        is not None
    )


def blames_sampling_param(msg: str, param: str) -> bool:
    """True when a 400 names ``param`` as a rejected request field.

    Anthropic-style field paths (``temperature:``), OpenAI-style quoted names
    (``Unsupported parameter: 'temperature'``) and Azure's unquoted
    ``Unrecognized request argument supplied: reasoning_effort`` count. The
    ``invalid_request_error`` type every OpenAI 400 carries is not a reason.
    """
    if blames_field_path(msg, param):
        return True
    lowered = msg.lower()
    name = re.escape(param)
    if re.search(rf"['\"]{name}['\"]|argument supplied:\s*{name}\b", lowered) is None:
        return False
    lowered = lowered.replace("invalid_request_error", "")
    return any(
        phrase in lowered
        for phrase in (
            "unsupported",
            "not supported",
            "not permitted",
            "unknown",
            "unrecognized",
            "invalid",
        )
    )


def remember_rejected_sampling(
    provider: str,
    model: str,
    params: Mapping[str, object] | Iterable[str],
) -> None:
    """Record params to omit on later requests. Logs once per new name.

    Pass a mapping of name to the value that was sent so only that value is
    omitted later. A plain list of names omits the param whatever its value.
    """
    items = (
        list(params.items()) if isinstance(params, Mapping) else [(name, _ANY) for name in params]
    )
    items = [(name, value) for name, value in items if name]
    if not provider or not model or not items:
        return
    key = (provider, model)
    with _lock:
        bucket = _rejected.setdefault(key, {})
        fresh = [name for name, _ in items if name not in bucket]
        for name, value in items:
            values = bucket.setdefault(name, [])
            if not any(v is value or v == value for v in values):
                values.append(value)
    if fresh:
        _log.warning(
            "sampling params rejected by the model; omitting them on later turns. "
            "Change model.reasoning_effort or model.temperature %s",
            kv(provider=provider, model=model, dropped=sorted(fresh)),
        )


def _matches(values: list[object], value: object) -> bool:
    return value is _ANY or any(v is _ANY or v == value for v in values)


def omit_known_rejected(provider: str, model: str, kwargs: dict[str, object]) -> None:
    """Drop params this provider and model already rejected with the same value."""
    if not provider or not model:
        return
    with _lock:
        known = {
            name: list(values) for name, values in _rejected.get((provider, model), {}).items()
        }
    for name, values in known.items():
        if name in kwargs and _matches(values, kwargs[name]):
            del kwargs[name]


def was_rejected(provider: str, model: str, param: str, value: object = _ANY) -> bool:
    """True when ``param`` was rejected (with ``value``, when given)."""
    with _lock:
        values = _rejected.get((provider, model), {}).get(param)
        return values is not None and _matches(values, value)


def clear_rejected_sampling() -> None:
    """Drop the memory. Tests use this so cases do not leak across each other."""
    with _lock:
        _rejected.clear()
