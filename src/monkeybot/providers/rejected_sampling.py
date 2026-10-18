"""Remember sampling fields a model rejected so the next turn does not resend them."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable

from monkeybot.core.logging_utils import kv

_log = logging.getLogger(__name__)

_ERROR_CODE_400 = re.compile(r"error code:\s*400\b", re.IGNORECASE)

_rejected: dict[tuple[str, str], set[str]] = {}
_lock = threading.Lock()


def is_status_400(exc: BaseException) -> bool:
    """True for an HTTP 400.

    A numeric ``status_code`` wins. Otherwise only the SDK's ``Error code: 400``
    prefix counts, so a limit like ``max_tokens 4000`` is not treated as a 400.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 400
    return _ERROR_CODE_400.search(str(exc)) is not None


def remember_rejected_sampling(provider: str, model: str, params: Iterable[str]) -> None:
    """Record params to omit on later requests. Logs once per new name."""
    names = [name for name in params if name]
    if not provider or not model or not names:
        return
    key = (provider, model)
    with _lock:
        bucket = _rejected.setdefault(key, set())
        fresh = [name for name in names if name not in bucket]
        bucket.update(names)
    if fresh:
        _log.warning(
            "sampling params rejected by the model; omitting them on later turns. "
            "Change model.reasoning_effort or model.temperature %s",
            kv(provider=provider, model=model, dropped=sorted(fresh)),
        )


def omit_known_rejected(provider: str, model: str, kwargs: dict[str, object]) -> None:
    """Drop params this provider and model already rejected."""
    if not provider or not model:
        return
    with _lock:
        known = set(_rejected.get((provider, model), ()))
    for name in known:
        kwargs.pop(name, None)


def was_rejected(provider: str, model: str, param: str) -> bool:
    with _lock:
        return param in _rejected.get((provider, model), ())


def clear_rejected_sampling() -> None:
    """Drop the memory. Tests use this so cases do not leak across each other."""
    with _lock:
        _rejected.clear()
