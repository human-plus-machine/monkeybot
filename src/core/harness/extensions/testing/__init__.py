"""Public testing helpers for third-party backend implementers.

This module exposes one ``*_contract_suite`` callable per extension surface
(Checkpointer, MemoryStore, JobStorage, IdentitySource, SecretResolver,
ModelProvider). Consumers implementing a new backend wire their factory into
the corresponding suite from a pytest test function and the framework drives
the same invariants the shipped backends are validated against.

The invariants themselves live in ``tests/harness/extensions/contracts/`` and
are the single source of truth. The ``*_contract_suite`` functions here are
thin, importable wrappers so consumer projects do **not** need to depend on
monkey-bot's internal test layout.

Usage from a consumer project::

    # tests/test_my_redis_ckpt_contract.py
    from emonk.harness.extensions.testing import checkpointer_contract_suite
    from my_pkg.redis_ckpt import RedisCheckpointer


    def test_redis_checkpointer_matches_contract() -> None:
        def factory() -> RedisCheckpointer:
            return RedisCheckpointer(url="redis://localhost:6379")

        checkpointer_contract_suite(factory)

Each call runs every invariant sequentially against a *fresh* backend
instance (the factory is invoked once per invariant). Invariants that are
logically inapplicable to a given backend (for example TTL invariants on a
store that does not advertise TTL) are skipped, matching the internal suite.
"""

# BEGIN harness-extensibility story 9
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pydantic import SecretStr

from ..base import (
    Checkpointer,
    IdentitySource,
    JobStorage,
    MemoryStore,
    ModelProvider,
    SecretResolver,
)
from ..errors import (
    CheckpointMissing,
    IdentityNotFound,
    SecretNotFound,
)
from ..values import MemoryPatch

BackendFactory = Callable[[], Any]
_AsyncInvariant = Callable[[BackendFactory], Awaitable[None]]


class ContractSkipped(Exception):  # noqa: N818 - reads as "contract was skipped", not an error
    """Raised by an invariant when it is logically inapplicable to the backend.

    The runner catches this and continues with the next invariant instead of
    failing, mirroring pytest's ``skip`` semantics without requiring pytest.
    """


def _run(coro: Awaitable[None]) -> None:
    """Execute ``coro`` on a fresh event loop.

    We deliberately use :func:`asyncio.run` (and not ``asyncio.get_event_loop``)
    so each invariant gets a clean loop even if the caller is running inside a
    pytest-asyncio session.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    # Running inside an existing loop (e.g. pytest-asyncio `mode=auto`); schedule
    # the coroutine on a private loop in a dedicated thread to keep isolation.
    import threading

    error: list[BaseException] = []

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(coro)
        except BaseException as exc:
            error.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]


def _run_suite(
    invariants: list[tuple[str, _AsyncInvariant]],
    factory: BackendFactory,
) -> None:
    """Execute every invariant in sequence; raise on the first real failure."""
    for name, invariant in invariants:
        try:
            _run(invariant(factory))
        except ContractSkipped:
            continue
        except AssertionError as exc:
            raise AssertionError(f"{name}: {exc}") from exc


# ---------------------------------------------------------------------------
# Checkpointer invariants (CKPT-C-01 … CKPT-C-07)
# ---------------------------------------------------------------------------


async def _ckpt_c_01(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    ref_a = await ckpt.write("session-1", {"x": 1}, reason="turn_end")
    ref_b = await ckpt.write("session-1", {"x": 2}, reason="turn_end")
    assert ref_a.checkpoint_id != ref_b.checkpoint_id
    assert ref_a.checkpoint_id < ref_b.checkpoint_id
    assert await ckpt.read("session-1", ref_a.checkpoint_id) == {"x": 1}


async def _ckpt_c_02(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    await ckpt.write("s", {"v": 1})
    await ckpt.write("s", {"v": 2})
    await ckpt.write("s", {"v": 3})
    assert await ckpt.read("s") == {"v": 3}


async def _ckpt_c_03(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    ref = await ckpt.write("s", {"payload": "value"})
    assert await ckpt.read("s", ref.checkpoint_id) == {"payload": "value"}


async def _ckpt_c_04(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    refs = [await ckpt.write("s", {"i": i}) for i in range(5)]
    listed = await ckpt.list("s", limit=3)
    assert len(listed) == 3
    assert [r.checkpoint_id for r in listed] == [
        refs[-1].checkpoint_id,
        refs[-2].checkpoint_id,
        refs[-3].checkpoint_id,
    ]


async def _ckpt_c_05(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    ref = await ckpt.write("s", {"v": 1})
    await ckpt.delete_session("s")
    assert await ckpt.read("s") is None
    try:
        await ckpt.read("s", ref.checkpoint_id)
    except CheckpointMissing:
        return
    raise AssertionError("expected CheckpointMissing after delete_session")


async def _ckpt_c_06(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    refs = await asyncio.gather(*[ckpt.write("s", {"i": i}) for i in range(100)])
    ids = {ref.checkpoint_id for ref in refs}
    assert len(ids) == 100


async def _ckpt_c_07(factory: BackendFactory) -> None:
    ckpt: Checkpointer = factory()
    payload = {"blob": "a" * 1_000_000}
    ref = await ckpt.write("s", payload)
    assert await ckpt.read("s", ref.checkpoint_id) == payload


_CKPT_INVARIANTS: list[tuple[str, _AsyncInvariant]] = [
    ("CKPT-C-01", _ckpt_c_01),
    ("CKPT-C-02", _ckpt_c_02),
    ("CKPT-C-03", _ckpt_c_03),
    ("CKPT-C-04", _ckpt_c_04),
    ("CKPT-C-05", _ckpt_c_05),
    ("CKPT-C-06", _ckpt_c_06),
    ("CKPT-C-07", _ckpt_c_07),
]


def checkpointer_contract_suite(backend_factory: Callable[[], Checkpointer]) -> None:
    """Run ``CKPT-C-01 … CKPT-C-07`` against ``backend_factory``.

    Args:
        backend_factory: Zero-argument callable returning a fresh
            :class:`Checkpointer` instance. The factory is invoked once per
            invariant so backends that hold state (open DB connections,
            in-process dicts, etc.) start each case clean.

    Raises:
        AssertionError: On the first failing invariant; the message is prefixed
            with the invariant id (for example ``CKPT-C-04: ...``).
    """
    _run_suite(_CKPT_INVARIANTS, backend_factory)


# ---------------------------------------------------------------------------
# MemoryStore invariants (MEM-C-01 … MEM-C-07; MEM-C-08 requires langgraph)
# ---------------------------------------------------------------------------


async def _mem_c_01(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.updated_at >= item.created_at


async def _mem_c_02(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    await store.put(("u",), "k", {"v": 1})
    first = await store.get(("u",), "k")
    assert first is not None
    await asyncio.sleep(0.01)
    await store.put(("u",), "k", {"v": 2})
    second = await store.get(("u",), "k")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert second.value == {"v": 2}


async def _mem_c_03(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def _mem_c_04(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    notes = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in notes} == {"a", "c"}


async def _mem_c_05(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    result = await store.list_namespaces(("a",))
    assert ("a", "1") in result
    assert ("a", "2") in result
    assert ("b", "1") not in result


async def _mem_c_06(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    caps = store.capabilities()
    assert caps.namespace_listing is True


async def _mem_c_07(factory: BackendFactory) -> None:
    store: MemoryStore = factory()
    caps = store.capabilities()
    if not caps.ttl:
        raise ContractSkipped("backend does not advertise TTL support")
    await store.put(("u",), "ephemeral", {"v": 1}, ttl=timedelta(milliseconds=50))
    await asyncio.sleep(0.15)
    assert await store.get(("u",), "ephemeral") is None


_MEM_INVARIANTS: list[tuple[str, _AsyncInvariant]] = [
    ("MEM-C-01", _mem_c_01),
    ("MEM-C-02", _mem_c_02),
    ("MEM-C-03", _mem_c_03),
    ("MEM-C-04", _mem_c_04),
    ("MEM-C-05", _mem_c_05),
    ("MEM-C-06", _mem_c_06),
    ("MEM-C-07", _mem_c_07),
]


def memory_store_contract_suite(backend_factory: Callable[[], MemoryStore]) -> None:
    """Run ``MEM-C-01 … MEM-C-07`` against ``backend_factory``.

    MEM-C-08 (LangGraph ``BaseStore`` adapter compliance) is not driven here to
    avoid a hard ``langgraph`` dependency in consumer test environments; wire
    it manually if relevant (see ``docs/extending-the-harness.md``).
    """
    _run_suite(_MEM_INVARIANTS, backend_factory)


# ---------------------------------------------------------------------------
# JobStorage invariants (JOB-C-01 … JOB-C-04)
# ---------------------------------------------------------------------------


async def _job_c_01(factory: BackendFactory) -> None:
    storage: JobStorage = factory()
    await storage.save_jobs([{"job_id": "race", "payload": {}}])
    results = await asyncio.gather(*[storage.claim_job("race") for _ in range(16)])
    assert results.count(True) == 1
    assert results.count(False) == 15


async def _job_c_02(factory: BackendFactory) -> None:
    storage: JobStorage = factory()
    await storage.save_jobs([{"job_id": "leased", "payload": {}}])
    assert await storage.claim_job("leased", lease_duration_seconds=60)
    assert not await storage.claim_job("leased", lease_duration_seconds=60)


async def _job_c_03(factory: BackendFactory) -> None:
    storage: JobStorage = factory()
    await storage.save_jobs([{"job_id": "reclaim", "payload": {}}])
    assert await storage.claim_job("reclaim")
    await storage.release_job("reclaim")
    assert await storage.claim_job("reclaim")


async def _job_c_04(factory: BackendFactory) -> None:
    storage: JobStorage = factory()
    jobs = [
        {"job_id": "a", "payload": {"n": 1}},
        {"job_id": "b", "payload": {"n": 2}},
    ]
    await storage.save_jobs(jobs)
    loaded = await storage.load_jobs()
    ids = {job["job_id"] for job in loaded}
    assert ids == {"a", "b"}


_JOB_INVARIANTS: list[tuple[str, _AsyncInvariant]] = [
    ("JOB-C-01", _job_c_01),
    ("JOB-C-02", _job_c_02),
    ("JOB-C-03", _job_c_03),
    ("JOB-C-04", _job_c_04),
]


def job_storage_contract_suite(backend_factory: Callable[[], JobStorage]) -> None:
    """Run ``JOB-C-01 … JOB-C-04`` against ``backend_factory``."""
    _run_suite(_JOB_INVARIANTS, backend_factory)


# ---------------------------------------------------------------------------
# IdentitySource invariants (ID-C-01 … ID-C-04)
# ---------------------------------------------------------------------------


def _principal(name: str = "alice") -> Any:
    from ...events import Principal

    return Principal(kind="user", id=name)


async def _id_c_01(factory: BackendFactory) -> None:
    source: IdentitySource = factory()
    identity = await source.load(principal=_principal("alice"))
    assert identity.principal_id == "alice"
    assert identity.soul
    assert identity.rules


async def _id_c_02(factory: BackendFactory) -> None:
    source: IdentitySource = factory()
    try:
        await source.load(principal=_principal("nobody"))
    except IdentityNotFound:
        return
    raise AssertionError("expected IdentityNotFound for unknown principal")


async def _id_c_03(factory: BackendFactory) -> None:
    source: IdentitySource = factory()
    principal = _principal("alice")
    try:
        await source.write_memory(
            principal=principal,
            patch=MemoryPatch(target="MEMORY.md", operation="replace", content="hello"),
        )
    except NotImplementedError:
        raise ContractSkipped("backend does not support write_memory") from None
    identity = await source.load(principal=principal)
    assert "hello" in identity.memory


async def _id_c_04(factory: BackendFactory) -> None:
    source: IdentitySource = factory()
    principal = _principal("alice")
    first = await source.load(principal=principal, session_id="s1")
    second = await source.load(principal=principal, session_id="s1")
    assert first.principal_id == second.principal_id
    assert first.soul == second.soul
    assert first.rules == second.rules


_ID_INVARIANTS: list[tuple[str, _AsyncInvariant]] = [
    ("ID-C-01", _id_c_01),
    ("ID-C-02", _id_c_02),
    ("ID-C-03", _id_c_03),
    ("ID-C-04", _id_c_04),
]


def identity_source_contract_suite(backend_factory: Callable[[], IdentitySource]) -> None:
    """Run ``ID-C-01 … ID-C-04`` against ``backend_factory``.

    Backends must seed a principal ``alice`` with non-empty SOUL and RULES
    content before running the suite; ID-C-02 requires ``nobody`` to be
    absent.
    """
    _run_suite(_ID_INVARIANTS, backend_factory)


# ---------------------------------------------------------------------------
# SecretResolver invariants (SEC-C-01, SEC-C-02, SEC-C-04)
# ---------------------------------------------------------------------------


async def _sec_c_01(factory: BackendFactory) -> None:
    resolver: SecretResolver = factory()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == "the-secret"


async def _sec_c_02(factory: BackendFactory) -> None:
    resolver: SecretResolver = factory()
    try:
        await resolver.resolve("__MISSING__")
    except SecretNotFound:
        return
    raise AssertionError("expected SecretNotFound for unknown handle")


async def _sec_c_04(factory: BackendFactory) -> None:
    resolver: SecretResolver = factory()
    value = await resolver.resolve("KNOWN_HANDLE")
    assert "the-secret" not in repr(value)
    assert "the-secret" not in str(value)


_SEC_INVARIANTS: list[tuple[str, _AsyncInvariant]] = [
    ("SEC-C-01", _sec_c_01),
    ("SEC-C-02", _sec_c_02),
    ("SEC-C-04", _sec_c_04),
]


def secret_resolver_contract_suite(backend_factory: Callable[[], SecretResolver]) -> None:
    """Run ``SEC-C-01``, ``SEC-C-02`` and ``SEC-C-04`` against ``backend_factory``.

    The factory must return a resolver that knows the handle ``KNOWN_HANDLE``
    bound to the literal string ``"the-secret"``. SEC-C-03 (composite chain)
    is not driven by this suite because it exercises a composition pattern
    rather than a single-backend contract.
    """
    _run_suite(_SEC_INVARIANTS, backend_factory)


# ---------------------------------------------------------------------------
# ModelProvider invariants (MP-C-01, MP-C-02)
# ---------------------------------------------------------------------------


async def _mp_c_01(factory: BackendFactory) -> None:
    from langchain_core.language_models import BaseChatModel

    from ...specs import AgentSpec

    provider: ModelProvider = factory()
    model = provider.build(AgentSpec(name="mock"))
    assert isinstance(model, BaseChatModel)
    out = model.invoke("hello")
    assert out is not None


async def _mp_c_02(factory: BackendFactory) -> None:
    provider: ModelProvider = factory()
    caps = provider.capabilities()
    assert isinstance(caps.tool_calling, bool)


_MP_INVARIANTS: list[tuple[str, _AsyncInvariant]] = [
    ("MP-C-01", _mp_c_01),
    ("MP-C-02", _mp_c_02),
]


def model_provider_contract_suite(backend_factory: Callable[[], ModelProvider]) -> None:
    """Run ``MP-C-01`` and ``MP-C-02`` against ``backend_factory``.

    MP-C-03 (Bedrock Converse schema) and MP-C-04 (streaming) are backend
    specific; see the internal contract module for their definitions.
    """
    _run_suite(_MP_INVARIANTS, backend_factory)


__all__ = [
    "ContractSkipped",
    "checkpointer_contract_suite",
    "identity_source_contract_suite",
    "job_storage_contract_suite",
    "memory_store_contract_suite",
    "model_provider_contract_suite",
    "secret_resolver_contract_suite",
]
# END harness-extensibility story 9
