"""Shared fixtures for the per-surface contract suite.

Story 1 seeds each fixture with just the ``"mock"`` reference backend from
``src.core.harness.extensions._mocks``. Subsequent stories extend the
``CONTRACT_BACKENDS`` lists so the same test modules run against every
shipped backend (Postgres, Mongo, Firestore, S3, etc.).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from src.core.harness.extensions._mocks import (
    MockCheckpointer,
    MockIdentitySource,
    MockMemoryStore,
    MockModelProvider,
    MockSecretResolver,
)

# isort: off
from .fixtures.job_storage_backends import JOB_STORAGE_FACTORIES

# BEGIN harness-extensibility story 5
from .fixtures.identity_source_backends import IDENTITY_SOURCE_FACTORIES

# END harness-extensibility story 5
# BEGIN harness-extensibility story 6
from .fixtures.secret_resolver_backends import SECRET_RESOLVER_FACTORIES

# END harness-extensibility story 6
# BEGIN harness-extensibility story 7
from .fixtures.model_provider_backends import MODEL_PROVIDER_FACTORIES

# END harness-extensibility story 7
# isort: on

CHECKPOINTER_BACKENDS: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockCheckpointer()),
]
MEMORY_STORE_BACKENDS: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockMemoryStore()),
]
JOB_STORAGE_BACKENDS: list[tuple[str, Callable[[], Any]]] = list(JOB_STORAGE_FACTORIES)
IDENTITY_SOURCE_BACKENDS: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockIdentitySource()),
    # BEGIN harness-extensibility story 5
    *IDENTITY_SOURCE_FACTORIES,
    # END harness-extensibility story 5
]
SECRET_RESOLVER_BACKENDS: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockSecretResolver({"KNOWN_HANDLE": "the-secret"})),
    # BEGIN harness-extensibility story 6
    *SECRET_RESOLVER_FACTORIES,
    # END harness-extensibility story 6
]
MODEL_PROVIDER_BACKENDS: list[tuple[str, Callable[[], Any]]] = [
    ("mock", lambda: MockModelProvider()),
    # BEGIN harness-extensibility story 7
    *MODEL_PROVIDER_FACTORIES,
    # END harness-extensibility story 7
]


def _id_fn(param: tuple[str, Callable[[], Any]]) -> str:
    return param[0]


@pytest.fixture(params=CHECKPOINTER_BACKENDS, ids=_id_fn)
def checkpointer_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    _, factory = request.param
    return factory


@pytest.fixture(params=MEMORY_STORE_BACKENDS, ids=_id_fn)
def memory_store_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    _, factory = request.param
    return factory


@pytest.fixture(params=JOB_STORAGE_BACKENDS, ids=_id_fn)
def job_storage_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    _, factory = request.param
    return factory


@pytest.fixture(params=IDENTITY_SOURCE_BACKENDS, ids=_id_fn)
def identity_source_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    _, factory = request.param
    return factory


@pytest.fixture(params=SECRET_RESOLVER_BACKENDS, ids=_id_fn)
def secret_resolver_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    _, factory = request.param
    return factory


@pytest.fixture(params=MODEL_PROVIDER_BACKENDS, ids=_id_fn)
def model_provider_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    _, factory = request.param
    return factory
