"""Unit tests for the six extension ABCs."""

from __future__ import annotations

import pytest

from src.core.harness.extensions.base import (
    Checkpointer,
    IdentitySource,
    JobStorage,
    MemoryStore,
    ModelProvider,
    SecretResolver,
)
from src.core.harness.extensions.registry import Registry


@pytest.mark.parametrize(
    "abc_cls,expected_kind",
    [
        (Checkpointer, "checkpointer"),
        (MemoryStore, "memory_store"),
        (JobStorage, "job_storage"),
        (IdentitySource, "identity_source"),
        (SecretResolver, "secret_resolver"),
        (ModelProvider, "model_provider"),
    ],
)
def test_abc_has_registry_of_expected_kind(abc_cls: type, expected_kind: str) -> None:
    reg = abc_cls.registry
    assert isinstance(reg, Registry)
    assert reg.kind == expected_kind


@pytest.mark.parametrize(
    "abc_cls",
    [Checkpointer, MemoryStore, JobStorage, IdentitySource, SecretResolver, ModelProvider],
)
def test_abc_is_non_instantiable(abc_cls: type) -> None:
    with pytest.raises(TypeError):
        abc_cls()  # type: ignore[call-arg]


def test_checkpointer_has_expected_methods() -> None:
    expected = {"write", "read", "list", "delete_session", "gc"}
    assert expected.issubset(set(dir(Checkpointer)))


def test_memory_store_has_expected_methods() -> None:
    expected = {
        "put",
        "get",
        "search",
        "delete",
        "list_namespaces",
        "capabilities",
        "as_langgraph_store",
    }
    assert expected.issubset(set(dir(MemoryStore)))


def test_job_storage_has_expected_methods() -> None:
    expected = {"load_jobs", "save_jobs", "claim_job", "release_job", "get_job"}
    assert expected.issubset(set(dir(JobStorage)))


def test_identity_source_has_expected_methods() -> None:
    expected = {"load", "write_memory"}
    assert expected.issubset(set(dir(IdentitySource)))


def test_secret_resolver_has_expected_methods() -> None:
    assert "resolve" in dir(SecretResolver)


def test_model_provider_has_expected_methods() -> None:
    expected = {"build", "capabilities"}
    assert expected.issubset(set(dir(ModelProvider)))


def test_memory_store_capabilities_default() -> None:
    class _Dummy(MemoryStore):
        async def put(self, namespace, key, value, *, ttl=None):
            pass

        async def get(self, namespace, key):
            return None

        async def search(self, namespace, *, query=None, filter=None, limit=10):
            return []

        async def delete(self, namespace, key):
            pass

        async def list_namespaces(self, prefix=()):
            return []

    d = _Dummy()
    caps = d.capabilities()
    assert caps.keyword_search is True
    assert caps.vector_search is False


def test_memory_store_as_langgraph_store_default_raises() -> None:
    class _Dummy(MemoryStore):
        async def put(self, namespace, key, value, *, ttl=None):
            pass

        async def get(self, namespace, key):
            return None

        async def search(self, namespace, *, query=None, filter=None, limit=10):
            return []

        async def delete(self, namespace, key):
            pass

        async def list_namespaces(self, prefix=()):
            return []

    d = _Dummy()
    with pytest.raises(NotImplementedError):
        d.as_langgraph_store()


def test_model_provider_capabilities_default() -> None:
    class _Dummy(ModelProvider):
        def build(self, spec):
            raise RuntimeError("noop")

    d = _Dummy()
    caps = d.capabilities()
    assert caps.tool_calling is True
    assert caps.streaming is True
    assert caps.max_context_tokens == 128_000


@pytest.mark.asyncio
async def test_checkpointer_gc_default_raises_not_implemented() -> None:
    from datetime import timedelta

    class _Dummy(Checkpointer):
        async def write(self, session_id, state, *, reason="turn_end"):
            raise NotImplementedError

        async def read(self, session_id, checkpoint_id=None):
            return None

        async def list(self, session_id, *, limit=100):
            return []

        async def delete_session(self, session_id):
            pass

    d = _Dummy()
    with pytest.raises(NotImplementedError):
        await d.gc(timedelta(seconds=1))


@pytest.mark.asyncio
async def test_job_storage_get_job_default_raises() -> None:
    class _Dummy(JobStorage):
        async def load_jobs(self):
            return []

        async def save_jobs(self, jobs):
            pass

        async def claim_job(self, job_id, lease_duration_seconds=300):
            return True

        async def release_job(self, job_id):
            pass

    d = _Dummy()
    with pytest.raises(NotImplementedError):
        await d.get_job("x")


@pytest.mark.asyncio
async def test_identity_source_write_memory_default_raises() -> None:
    from src.core.harness.events import Principal
    from src.core.harness.extensions.values import MemoryPatch

    class _Dummy(IdentitySource):
        async def load(self, *, principal, session_id=None):
            raise NotImplementedError

    d = _Dummy()
    with pytest.raises(NotImplementedError):
        await d.write_memory(
            principal=Principal(kind="anonymous"),
            patch=MemoryPatch(target="MEMORY.md", operation="append", content="x"),
        )
