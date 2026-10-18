"""SDK HTTP clients cached per running event loop and endpoint.

One client per loop and endpoint. The model id and the session-affinity header
stay on each request, so a later turn can call a different model on the same
warm connection. A client is not reused after its event loop closes, and code
with no running loop gets a fresh client each time.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import threading
import weakref
from collections.abc import Callable
from typing import Any

from monkeybot.core.llm.provider import ProviderCallHints

_log = logging.getLogger(__name__)

_lock = threading.Lock()
# Clients kept per key and loop. Room for a key rotation while sessions built
# before it still hold the old credentials, without growing on every rotation.
_MAX_CLIENTS_PER_KEY = 4
_clients: dict[tuple[Any, ...], list[_Slot]] = {}


class _Slot:
    """A client, the loop that may use it, and a digest of what built it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, fingerprint: str, client: Any) -> None:
        self.client = client
        self.fingerprint = fingerprint
        self._loop = weakref.ref(loop)

    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The live loop, or None when that loop is gone."""
        loop = self._loop()
        if loop is None or loop.is_closed():
            return None
        return loop


def _digest(values: tuple[Any, ...]) -> str:
    return hashlib.sha256(repr(values).encode()).hexdigest()


def sdk_env(*prefixes: str) -> tuple[tuple[str, str], ...]:
    """Env vars an SDK constructor may read, as sorted ``(name, value)`` pairs.

    Pass this as ``fingerprint`` so a change to any of them rebuilds the client,
    including variables a later SDK release starts reading.
    """
    return tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith(prefixes)))


def cached_sdk_client(
    key: tuple[Any, ...],
    factory: Callable[[], Any],
    *,
    fingerprint: tuple[Any, ...] = (),
) -> Any:
    """Return the client for ``key`` on this loop, building it once per loop.

    ``key`` names an endpoint that may be used alongside others (a base URL, a
    region). ``fingerprint`` holds the credentials and settings the client was
    built from. It is hashed before it is stored. Each key keeps the most recently
    used ``_MAX_CLIENTS_PER_KEY`` fingerprints per loop, and the oldest is dropped
    without closing because a request may still be streaming on it.

    With no running loop the client is not cached.
    """
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        return factory()
    digest = _digest(fingerprint)
    with _lock:
        slots = _clients.get(key, [])
        for slot in slots:
            if slot.loop() is current and slot.fingerprint == digest:
                # Most recently used last, so eviction drops the stale ones.
                slots.remove(slot)
                slots.append(slot)
                return slot.client
    # Built outside the lock: constructors can do blocking auth lookups. Only
    # this loop's thread fills this loop's slots, so no other thread races it.
    client = factory()
    with _lock:
        live = [slot for slot in _clients.get(key, ()) if slot.loop() is not None]
        others = [slot for slot in live if slot.loop() is not current]
        mine = [slot for slot in live if slot.loop() is current]
        recent = mine[-(_MAX_CLIENTS_PER_KEY - 1) :]
        _clients[key] = [*others, *recent, _Slot(current, digest, client)]
    return client


def _take_clients() -> list[Any]:
    with _lock:
        clients = [slot.client for slots in _clients.values() for slot in slots]
        _clients.clear()
        return clients


async def _aclose_client(client: Any) -> None:
    """Close one SDK client.

    google-genai's async pool is ``Client.aclose``; its sync ``close`` leaves
    that pool open. OpenAI and Anthropic only have async ``close``.
    """
    aclose = getattr(client, "aclose", None)
    if callable(aclose):
        result = aclose()
        if inspect.isawaitable(result):
            await result
        return
    close = getattr(client, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _aclose_all(clients: list[Any]) -> None:
    for client in clients:
        try:
            await _aclose_client(client)
        except Exception:
            _log.warning("SDK client close failed: %s", type(client).__name__, exc_info=True)


async def aclose_sdk_clients() -> None:
    """Close and drop every cached SDK client."""
    await _aclose_all(_take_clients())


def clear_sdk_clients() -> None:
    """Close and drop cached clients so a later call builds a new one.

    Raises RuntimeError inside a running loop, where closing would block it.
    Await ``aclose_sdk_clients`` there instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "clear_sdk_clients cannot run inside an event loop; await aclose_sdk_clients"
        )
    clients = _take_clients()
    if clients:
        asyncio.run(_aclose_all(clients))


def session_affinity_headers(hints: ProviderCallHints | None) -> dict[str, str] | None:
    """Per-request ``x-session-affinity`` header, or None when caching is off."""
    if hints is None or not hints.session_id or hints.cache_retention == "none":
        return None
    return {"x-session-affinity": hints.session_id}
