"""Tests for :class:`GCSIdentitySource` using in-memory fakes.

``google.cloud.storage`` is heavy and requires ADC, so these tests
monkey-patch the source's private ``_get_client`` hook with a fake that
records every ``get/put/delete`` in a simple dict.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.harness.events import Principal
from src.core.harness.extensions import GCSIdentitySource, IdentityNotFound
from src.core.harness.extensions.values import MemoryPatch

pytestmark = pytest.mark.asyncio


class _FakeBlob:
    def __init__(self, store: dict[str, str], name: str) -> None:
        self._store = store
        self._name = name

    def exists(self) -> bool:
        return self._name in self._store

    def download_as_text(self) -> str:
        return self._store[self._name]

    def upload_from_string(self, payload: str, content_type: str = "") -> None:  # noqa: ARG002
        self._store[self._name] = payload

    def delete(self) -> None:
        self._store.pop(self._name, None)


class _FakeBucket:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)


class _FakeClient:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def bucket(self, _name: str) -> _FakeBucket:
        return _FakeBucket(self._store)


def _seed(store: dict[str, str], principal_id: str = "alice", prefix: str = "") -> None:
    base = f"{prefix}{principal_id}"
    for name in ("SOUL", "IDENTITY", "USER", "INDEX", "RULES", "MEMORY", "HEARTBEAT"):
        store[f"{base}/{name}.md"] = f"{name.lower()}-body"


@pytest.fixture
def source() -> tuple[GCSIdentitySource, dict[str, str]]:
    store: dict[str, str] = {}
    src = GCSIdentitySource(bucket="fake")
    src._client = _FakeClient(store)  # type: ignore[attr-defined]
    return src, store


async def test_gcs_load_known_principal(source: tuple[GCSIdentitySource, dict[str, str]]) -> None:
    """A populated bucket yields a full identity."""
    src, store = source
    _seed(store, "alice")
    identity = await src.load(principal=Principal(kind="user", id="alice"))
    assert identity.source_backend == "gcs"
    assert identity.soul == "soul-body"


async def test_gcs_missing_principal_raises(
    source: tuple[GCSIdentitySource, dict[str, str]],
) -> None:
    """When no files exist for the principal, :class:`IdentityNotFound` fires."""
    src, _ = source
    with pytest.raises(IdentityNotFound):
        await src.load(principal=Principal(kind="user", id="nobody"))


async def test_gcs_write_memory_round_trip(
    source: tuple[GCSIdentitySource, dict[str, str]],
) -> None:
    """``write_memory`` updates are visible on the next load."""
    src, store = source
    _seed(store, "alice")
    await src.write_memory(
        principal=Principal(kind="user", id="alice"),
        patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
    )
    identity = await src.load(principal=Principal(kind="user", id="alice"))
    assert identity.memory == "hello"


async def test_gcs_rejects_empty_bucket() -> None:
    """Constructor refuses an empty bucket name."""
    with pytest.raises(ValueError):
        GCSIdentitySource(bucket="")


async def test_gcs_requires_sdk_when_client_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``google.cloud.storage`` installed, client init raises."""
    import builtins

    real_import = builtins.__import__

    def _fail_google(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name.startswith("google.cloud") or (
            name == "google" and fromlist and "cloud" in fromlist
        ):
            raise ImportError("google.cloud.storage not installed")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fail_google)
    src = GCSIdentitySource(bucket="fake")
    with pytest.raises((RuntimeError, ImportError)):
        src._get_client()  # type: ignore[attr-defined]
