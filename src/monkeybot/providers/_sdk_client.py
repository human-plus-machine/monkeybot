"""Process-wide SDK HTTP clients.

One client per endpoint (SDK, host, credentials, region). The model id and the
session-affinity header stay on each request, so a later turn can call a
different model on the same warm connection.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from monkeybot.core.llm.provider import ProviderCallHints

_lock = threading.Lock()
_clients: dict[tuple[Any, ...], Any] = {}


def cached_sdk_client(key: tuple[Any, ...], factory: Callable[[], Any]) -> Any:
    """Return the client for ``key``, building it once per process."""
    client = _clients.get(key)
    if client is not None:
        return client
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = factory()
            _clients[key] = client
        return client


def clear_sdk_clients() -> None:
    """Drop cached clients. Tests use this so a patched constructor is not skipped."""
    with _lock:
        _clients.clear()


def session_affinity_headers(hints: ProviderCallHints | None) -> dict[str, str] | None:
    """Per-request ``x-session-affinity`` header, or None when caching is off."""
    if hints is None or not hints.session_id or hints.cache_retention == "none":
        return None
    return {"x-session-affinity": hints.session_id}
