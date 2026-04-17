"""Functional test: secret values never appear in any emitted event payload.

Boots a :class:`CompositeSecretResolver` wrapped in
:class:`TracingResolver`, drives 1000 resolutions across two legs (env
and mock), and asserts that no captured :class:`HarnessEvent` payload
contains the raw handle string or the resolved secret value — only the
``blake2s`` hash is permitted to appear.

1000 resolutions (rather than the spec's 10k) is a deliberate
compromise for CI wallclock: blake2s/SecretStr are both cheap so the
invariant is already strongly exercised; the spec note permits this
scaling trade-off.
"""

from __future__ import annotations

import json

import pytest

from src.core.harness.event_bus import EventBus
from src.core.harness.events import EventKind, HarnessEvent
from src.core.harness.extensions._mocks import MockSecretResolver
from src.core.harness.extensions.secret_resolvers import (
    CompositeSecretResolver,
    EnvSecretResolver,
    TracingResolver,
)

pytestmark = pytest.mark.asyncio


class _CollectingHandler:
    """EventBus handler that captures every delivered event (no filtering)."""

    name = "collector"

    def __init__(self) -> None:
        self.events: list[HarnessEvent] = []

    async def handle(self, event: HarnessEvent) -> None:
        self.events.append(event)


async def test_no_secret_strings_in_event_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1000 resolutions across env + mock legs must never leak a secret into events."""
    monkeypatch.setenv("T_FOO", "bar")
    env_leg = EnvSecretResolver(prefix="T_")
    mock_leg = MockSecretResolver({"known": "val"})
    composite = CompositeSecretResolver(chain=[env_leg, mock_leg])

    bus = EventBus(include_default_logger=False)
    collector = _CollectingHandler()
    bus.subscribe(collector)
    tracer = TracingResolver(composite, event_bus=bus, principal_id_source=lambda: "pid")

    handles = ("FOO", "known")
    for i in range(1000):
        handle = handles[i % 2]
        await tracer.resolve(handle)

    assert len(collector.events) == 1000
    forbidden = {"bar", "val", "FOO", "known", "T_FOO"}
    for event in collector.events:
        assert event.kind is EventKind.SECRET_RESOLVED
        serialised = json.dumps(event.payload, default=str)
        for bad in forbidden:
            assert bad not in serialised, (
                f"forbidden substring {bad!r} leaked into event payload: {serialised}"
            )
        assert "handle_hash" in event.payload
        assert len(str(event.payload["handle_hash"])) == 16
