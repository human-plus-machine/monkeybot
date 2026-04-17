"""Tests for :class:`TracingResolver` (Story 6).

The tracer wraps an inner :class:`SecretResolver` and emits a
``secret.resolved`` :class:`HarnessEvent` on every **successful** resolve.
Critical invariants verified here:

* The emitted payload contains ``handle_hash`` but never the raw handle
  or secret value.
* Failures (including :class:`SecretNotFound`) NEVER emit an event.
* The inner resolver's return value is propagated unchanged.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import SecretStr

from src.core.harness.event_bus import EventBus
from src.core.harness.events import EventKind, HarnessEvent
from src.core.harness.extensions import SecretNotFound
from src.core.harness.extensions._mocks import MockSecretResolver
from src.core.harness.extensions.secret_resolvers import TracingResolver

pytestmark = pytest.mark.asyncio


class _CapturingHandler:
    """EventBus handler that records every delivered event in order."""

    name = "capturing"

    def __init__(self) -> None:
        self.events: list[HarnessEvent] = []

    async def handle(self, event: HarnessEvent) -> None:
        self.events.append(event)


async def test_payload_contains_handle_hash_only() -> None:
    """Success emits ``secret.resolved`` with a blake2s hash — never the handle."""
    handle = "DB_PASSWORD"
    inner = MockSecretResolver({handle: "s3cret-value"})
    bus = EventBus(include_default_logger=False)
    capture = _CapturingHandler()
    bus.subscribe(capture)
    tracer = TracingResolver(inner, event_bus=bus, principal_id_source=lambda: "pid")

    value = await tracer.resolve(handle)

    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "s3cret-value"
    assert len(capture.events) == 1
    event = capture.events[0]
    assert event.kind is EventKind.SECRET_RESOLVED
    payload = dict(event.payload)
    expected_hash = hashlib.blake2s(handle.encode(), digest_size=8).hexdigest()
    assert payload["handle_hash"] == expected_hash
    assert payload["resolver"] == "MockSecretResolver"
    assert payload["principal_id"] == "pid"
    assert isinstance(payload["latency_ms"], int)
    serialized = str(payload)
    assert handle not in serialized
    assert "s3cret-value" not in serialized


async def test_no_event_on_secret_not_found() -> None:
    """``SecretNotFound`` propagates without emitting any event."""
    inner = MockSecretResolver({})
    bus = EventBus(include_default_logger=False)
    capture = _CapturingHandler()
    bus.subscribe(capture)
    tracer = TracingResolver(inner, event_bus=bus, principal_id_source=lambda: "pid")

    with pytest.raises(SecretNotFound):
        await tracer.resolve("MISSING")

    assert capture.events == []


async def test_no_event_on_other_exception() -> None:
    """Arbitrary exceptions also propagate without an emission."""

    class _BoomResolver(MockSecretResolver):
        async def resolve(self, handle: str) -> SecretStr:  # type: ignore[override]
            raise RuntimeError("boom")

    bus = EventBus(include_default_logger=False)
    capture = _CapturingHandler()
    bus.subscribe(capture)
    tracer = TracingResolver(_BoomResolver(), event_bus=bus)

    with pytest.raises(RuntimeError):
        await tracer.resolve("HANDLE")

    assert capture.events == []


async def test_tracer_returns_inner_value_unchanged() -> None:
    """The tracer is transparent: the inner ``SecretStr`` instance is returned."""
    inner = MockSecretResolver({"K": "v"})
    tracer = TracingResolver(inner)
    value = await tracer.resolve("K")
    assert value.get_secret_value() == "v"


async def test_no_event_bus_still_records_last_payload() -> None:
    """When no bus is configured, ``last_payload`` exposes the sanitised payload for tests."""
    inner = MockSecretResolver({"HANDLE": "value"})
    tracer = TracingResolver(inner, event_bus=None, principal_id_source=lambda: "pid")

    await tracer.resolve("HANDLE")

    assert tracer.last_payload is not None
    payload = dict(tracer.last_payload)
    assert payload["handle_hash"] == hashlib.blake2s(
        b"HANDLE", digest_size=8
    ).hexdigest()
    assert payload["resolver"] == "MockSecretResolver"
    assert payload["principal_id"] == "pid"
    serialized = str(payload)
    assert "HANDLE" not in serialized
    assert "value" not in serialized


async def test_default_principal_source_is_callable() -> None:
    """The default principal source falls back to an empty string when no scope is active."""
    inner = MockSecretResolver({"H": "v"})
    tracer = TracingResolver(inner)

    await tracer.resolve("H")

    assert tracer.last_payload is not None
    assert isinstance(tracer.last_payload["principal_id"], str)
