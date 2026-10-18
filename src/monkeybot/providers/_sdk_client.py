"""SDK HTTP clients cached per running event loop and endpoint.

One client per loop, host, and credentials. The model id and the
session-affinity header stay on each request, so a later turn can call a
different model on the same warm connection. A client is not reused after its
event loop closes.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import weakref
from collections.abc import Callable
from typing import Any

from monkeybot.core.llm.provider import ProviderCallHints

_lock = threading.Lock()
# Clients built from synchronous code (no running loop) share this owner.
_SYNC = object()
_clients: dict[tuple[Any, ...], list[_Slot]] = {}


class _Slot:
    """A client plus a weak reference to the loop that may use it."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None, client: Any) -> None:
        self.client = client
        self._sync = loop is None
        self._loop: weakref.ref[asyncio.AbstractEventLoop] | None = (
            weakref.ref(loop) if loop is not None else None
        )

    def owner(self) -> asyncio.AbstractEventLoop | object | None:
        """The live loop, the sync sentinel, or None when that loop is gone."""
        if self._sync:
            return _SYNC
        assert self._loop is not None
        loop = self._loop()
        if loop is None or loop.is_closed():
            return None
        return loop


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _current_owner() -> asyncio.AbstractEventLoop | object:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return _SYNC


def cached_sdk_client(
    key: tuple[Any, ...],
    factory: Callable[[], Any],
    *,
    secrets: tuple[str, ...] = (),
) -> Any:
    """Return the client for ``key`` on this loop, building it once per loop.

    ``secrets`` are hashed before they are stored. Pass API keys and auth
    tokens here instead of putting them in ``key``.
    """
    stored = (*key, *(_digest(secret) for secret in secrets))
    current = _current_owner()
    with _lock:
        kept: list[_Slot] = []
        found: Any = None
        for slot in _clients.get(stored, ()):
            owner = slot.owner()
            if owner is None:
                continue
            if found is None and owner is current:
                found = slot.client
            kept.append(slot)
        if found is None:
            found = factory()
            loop = current if isinstance(current, asyncio.AbstractEventLoop) else None
            kept.append(_Slot(loop, found))
        if kept:
            _clients[stored] = kept
        else:
            _clients.pop(stored, None)
        return found


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
        await _aclose_client(client)


async def aclose_sdk_clients() -> None:
    """Close and drop every cached SDK client."""
    await _aclose_all(_take_clients())


def clear_sdk_clients() -> None:
    """Drop cached clients so a later call builds a new one.

    When no loop is running, also close them. A running loop must await
    ``aclose_sdk_clients``; this function cannot block that loop.
    """
    clients = _take_clients()
    if not clients:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_aclose_all(clients))


def session_affinity_headers(hints: ProviderCallHints | None) -> dict[str, str] | None:
    """Per-request ``x-session-affinity`` header, or None when caching is off."""
    if hints is None or not hints.session_id or hints.cache_retention == "none":
        return None
    return {"x-session-affinity": hints.session_id}
