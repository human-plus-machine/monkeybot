"""Unit tests for the Story 1 linter rule additions (CKPT01 / CKPT02 / MEM01 / ID01 /
ID02 / SEC01 / MP01 / REG01 / IDENTITY-SMOKE).

Each rule runs against a synthetic ``HarnessConfig`` built directly from sub-specs.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.harness.extensions.base import Checkpointer
from src.core.harness.extensions.specs.checkpointer import (
    CheckpointerInMemorySpec,
    CheckpointerMongoSpec,
    CheckpointerPostgresSpec,
)
from src.core.harness.extensions.specs.identity_source import (
    IdentitySourceCallableSpec,
    IdentitySourceLocalFSSpec,
)
from src.core.harness.extensions.specs.memory_store import (
    MemoryStoreInMemorySpec,
    MemoryStorePostgresSpec,
)
from src.core.harness.extensions.specs.model_provider import (
    ModelProviderBedrockSpec,
)
from src.core.harness.extensions.specs.secret_resolver import (
    SecretResolverCompositeSpec,
    SecretResolverEnvSpec,
)
from src.core.harness.linter import (
    check_ckpt01_postgres_dsn_present,
    check_ckpt02_mongo_uri_present,
    check_id01_callable_requires_import_path,
    check_id02_cache_ttl_sane,
    check_identity_smoke,
    check_mem01_vector_search_supported,
    check_mp01_bedrock_model_id,
    check_reg01_no_shadowed_entries,
    check_sec01_composite_chain_non_empty,
)
from src.core.harness.specs import AgentSpec, HarnessConfig


def _mk_config(**overrides: Any) -> HarnessConfig:
    """Build a minimal ``HarnessConfig`` with per-test overrides."""
    return HarnessConfig(agent=AgentSpec(name="test"), **overrides)


def test_ckpt01_missing_postgres_dsn_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CKPT_DSN", raising=False)
    cfg = _mk_config(checkpointer=CheckpointerPostgresSpec(dsn_env="CKPT_DSN"))
    findings = check_ckpt01_postgres_dsn_present(cfg)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "CKPT01" in findings[0].message


def test_ckpt01_present_postgres_dsn_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CKPT_DSN", "postgresql://localhost/x")
    cfg = _mk_config(checkpointer=CheckpointerPostgresSpec(dsn_env="CKPT_DSN"))
    assert check_ckpt01_postgres_dsn_present(cfg) == []


def test_ckpt01_non_postgres_is_clean() -> None:
    cfg = _mk_config(checkpointer=CheckpointerInMemorySpec())
    assert check_ckpt01_postgres_dsn_present(cfg) == []


def test_ckpt02_missing_mongo_uri_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONGO_URI", raising=False)
    cfg = _mk_config(checkpointer=CheckpointerMongoSpec(uri_env="MONGO_URI"))
    findings = check_ckpt02_mongo_uri_present(cfg)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "CKPT02" in findings[0].message


def test_ckpt02_present_mongo_uri_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017")
    cfg = _mk_config(checkpointer=CheckpointerMongoSpec(uri_env="MONGO_URI"))
    assert check_ckpt02_mongo_uri_present(cfg) == []


def test_mem01_vector_search_on_unsupported_backend_is_error() -> None:
    cfg = _mk_config(
        memory_store=MemoryStoreInMemorySpec(require_vector_search=True),
    )
    findings = check_mem01_vector_search_supported(cfg)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "MEM01" in findings[0].message


def test_mem01_vector_search_on_pgvector_is_clean() -> None:
    cfg = _mk_config(
        memory_store=MemoryStorePostgresSpec(
            require_vector_search=True, enable_pgvector=True
        ),
    )
    assert check_mem01_vector_search_supported(cfg) == []


def test_mem01_not_required_is_clean() -> None:
    cfg = _mk_config(memory_store=MemoryStoreInMemorySpec())
    assert check_mem01_vector_search_supported(cfg) == []


def test_id01_callable_without_import_path_is_error() -> None:
    spec = IdentitySourceCallableSpec.model_construct(
        backend="callable", import_path=""
    )
    cfg = _mk_config(identity_source=spec)
    findings = check_id01_callable_requires_import_path(cfg)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "ID01" in findings[0].message


def test_id01_callable_with_import_path_is_clean() -> None:
    cfg = _mk_config(
        identity_source=IdentitySourceCallableSpec(import_path="pkg.mod:factory"),
    )
    assert check_id01_callable_requires_import_path(cfg) == []


def test_id02_short_cache_ttl_is_warning() -> None:
    cfg = _mk_config(
        identity_source=IdentitySourceLocalFSSpec(cache_ttl_seconds=1),
    )
    findings = check_id02_cache_ttl_sane(cfg)
    assert len(findings) == 1
    assert findings[0].level == "warning"
    assert "ID02" in findings[0].message


def test_id02_sane_cache_ttl_is_clean() -> None:
    cfg = _mk_config(identity_source=IdentitySourceLocalFSSpec(cache_ttl_seconds=300))
    assert check_id02_cache_ttl_sane(cfg) == []


def test_id02_no_identity_source_is_clean() -> None:
    cfg = _mk_config()
    assert check_id02_cache_ttl_sane(cfg) == []


def test_sec01_empty_composite_chain_is_error() -> None:
    cfg = _mk_config(secret_resolver=SecretResolverCompositeSpec(chain=[]))
    findings = check_sec01_composite_chain_non_empty(cfg)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "SEC01" in findings[0].message


def test_sec01_populated_composite_chain_is_clean() -> None:
    cfg = _mk_config(
        secret_resolver=SecretResolverCompositeSpec(chain=["env", "aws_secrets_manager"]),
    )
    assert check_sec01_composite_chain_non_empty(cfg) == []


def test_sec01_non_composite_resolver_is_clean() -> None:
    cfg = _mk_config(secret_resolver=SecretResolverEnvSpec())
    assert check_sec01_composite_chain_non_empty(cfg) == []


def test_mp01_bedrock_without_model_id_is_error() -> None:
    spec = ModelProviderBedrockSpec.model_construct(
        backend="bedrock", region="us-east-1", model_id=""
    )
    cfg = _mk_config(model_provider=spec)
    findings = check_mp01_bedrock_model_id(cfg)
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "MP01" in findings[0].message


def test_mp01_bedrock_with_model_id_is_clean() -> None:
    cfg = _mk_config(
        model_provider=ModelProviderBedrockSpec(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0"
        ),
    )
    assert check_mp01_bedrock_model_id(cfg) == []


def _dummy_factory(**_: Any) -> object:
    return object()


def test_reg01_reports_shadowed_entries() -> None:
    original_shadowed = list(Checkpointer.registry._shadowed)
    _dummy_factory.__qualname__ = "dummy"
    _dummy_factory.__module__ = "tests.harness.extensions.test_linter_additions"
    Checkpointer.registry.register(
        "reg01-shadow",
        _dummy_factory,
        source="builtin",
        module="builtin_mod",
    )
    Checkpointer.registry.register(
        "reg01-shadow",
        _dummy_factory,
        source="programmatic",
        module="prog_mod",
    )
    try:
        findings = check_reg01_no_shadowed_entries(_mk_config())
        assert any("reg01-shadow" in f.message or "reg01-shadow" in f.path for f in findings)
        assert all(f.level == "warning" for f in findings)
    finally:
        Checkpointer.registry._shadowed = original_shadowed
        Checkpointer.registry._entries.pop("reg01-shadow", None)
        Checkpointer.registry._factories.pop("reg01-shadow", None)


def test_identity_smoke_disabled_returns_empty() -> None:
    cfg = _mk_config()
    assert check_identity_smoke(cfg, enabled=False) == []


@pytest.mark.skip(reason="live identity probe not implemented in Story 1")
def test_identity_smoke_enabled_emits_finding() -> None:
    cfg = _mk_config()
    findings = check_identity_smoke(cfg, enabled=True)
    assert findings
