"""Unit tests for every discriminated-union extension spec."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.harness.extensions.specs import (
    CheckpointerFirestoreSpec,
    CheckpointerInMemorySpec,
    CheckpointerMongoSpec,
    CheckpointerPostgresSpec,
    CheckpointerSpec,
    IdentitySourceCallableSpec,
    IdentitySourceGCSSpec,
    IdentitySourceLocalFSSpec,
    IdentitySourceMongoSpec,
    IdentitySourcePostgresSpec,
    IdentitySourceS3Spec,
    IdentitySourceSpec,
    JobStorageFirestoreSpec,
    JobStorageJSONSpec,
    JobStorageMongoSpec,
    JobStoragePostgresSpec,
    JobStorageSpec,
    MemoryStoreFirestoreSpec,
    MemoryStoreGCSSpec,
    MemoryStoreInMemorySpec,
    MemoryStoreMongoSpec,
    MemoryStorePostgresSpec,
    MemoryStoreS3Spec,
    MemoryStoreSpec,
    ModelProviderAnthropicSpec,
    ModelProviderBedrockSpec,
    ModelProviderOllamaSpec,
    ModelProviderOpenAISpec,
    ModelProviderSpec,
    ModelProviderVertexSpec,
    SecretResolverAWSSpec,
    SecretResolverCompositeSpec,
    SecretResolverEnvSpec,
    SecretResolverGCPSpec,
    SecretResolverSpec,
)


@pytest.mark.parametrize(
    "data,expected_cls",
    [
        ({"backend": "in_memory"}, CheckpointerInMemorySpec),
        ({"backend": "firestore", "collection": "c"}, CheckpointerFirestoreSpec),
        ({"backend": "postgres", "dsn_env": "CKPT_DSN"}, CheckpointerPostgresSpec),
        ({"backend": "mongo", "uri_env": "MONGO_URI"}, CheckpointerMongoSpec),
    ],
)
def test_checkpointer_spec_dispatch(data: dict, expected_cls: type) -> None:
    spec = CheckpointerSpec.model_validate(data)
    assert isinstance(spec, expected_cls)


def test_checkpointer_spec_unknown_backend_lists_valid_literals() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CheckpointerSpec.model_validate({"backend": "unknown"})
    msg = str(exc_info.value)
    assert "in_memory" in msg or "firestore" in msg or "postgres" in msg


def test_checkpointer_spec_roundtrip() -> None:
    original = CheckpointerSpec.model_validate({"backend": "postgres", "dsn_env": "CKPT_DSN"})
    dump = original.model_dump()
    reloaded = CheckpointerSpec.model_validate(dump)
    assert reloaded == original


@pytest.mark.parametrize(
    "data,expected_cls",
    [
        ({"backend": "in_memory"}, MemoryStoreInMemorySpec),
        ({"backend": "firestore"}, MemoryStoreFirestoreSpec),
        ({"backend": "gcs", "bucket": "b"}, MemoryStoreGCSSpec),
        ({"backend": "s3", "bucket": "b"}, MemoryStoreS3Spec),
        ({"backend": "postgres"}, MemoryStorePostgresSpec),
        ({"backend": "mongo"}, MemoryStoreMongoSpec),
    ],
)
def test_memory_store_spec_dispatch(data: dict, expected_cls: type) -> None:
    spec = MemoryStoreSpec.model_validate(data)
    assert isinstance(spec, expected_cls)


def test_memory_store_spec_s3_requires_bucket() -> None:
    with pytest.raises(ValidationError):
        MemoryStoreSpec.model_validate({"backend": "s3"})


@pytest.mark.parametrize(
    "data,expected_cls",
    [
        ({"backend": "json"}, JobStorageJSONSpec),
        ({"backend": "firestore"}, JobStorageFirestoreSpec),
        ({"backend": "postgres"}, JobStoragePostgresSpec),
        ({"backend": "mongo"}, JobStorageMongoSpec),
    ],
)
def test_job_storage_spec_dispatch(data: dict, expected_cls: type) -> None:
    spec = JobStorageSpec.model_validate(data)
    assert isinstance(spec, expected_cls)


@pytest.mark.parametrize(
    "data,expected_cls",
    [
        ({"backend": "local_fs"}, IdentitySourceLocalFSSpec),
        ({"backend": "s3", "bucket": "b"}, IdentitySourceS3Spec),
        ({"backend": "gcs", "bucket": "b"}, IdentitySourceGCSSpec),
        ({"backend": "postgres"}, IdentitySourcePostgresSpec),
        ({"backend": "mongo"}, IdentitySourceMongoSpec),
        (
            {"backend": "callable", "import_path": "some.mod:fn"},
            IdentitySourceCallableSpec,
        ),
    ],
)
def test_identity_source_spec_dispatch(data: dict, expected_cls: type) -> None:
    spec = IdentitySourceSpec.model_validate(data)
    assert isinstance(spec, expected_cls)


@pytest.mark.parametrize(
    "data,expected_cls",
    [
        ({"backend": "env"}, SecretResolverEnvSpec),
        ({"backend": "aws_secrets_manager"}, SecretResolverAWSSpec),
        ({"backend": "gcp_secret_manager"}, SecretResolverGCPSpec),
        ({"backend": "composite", "chain": ["env"]}, SecretResolverCompositeSpec),
    ],
)
def test_secret_resolver_spec_dispatch(data: dict, expected_cls: type) -> None:
    spec = SecretResolverSpec.model_validate(data)
    assert isinstance(spec, expected_cls)


def test_secret_resolver_default_returns_env_spec() -> None:
    default = SecretResolverSpec.default()
    assert isinstance(default, SecretResolverEnvSpec)


@pytest.mark.parametrize(
    "data,expected_cls",
    [
        ({"backend": "vertex"}, ModelProviderVertexSpec),
        (
            {"backend": "bedrock", "model_id": "anthropic.claude-3-5-sonnet"},
            ModelProviderBedrockSpec,
        ),
        ({"backend": "openai"}, ModelProviderOpenAISpec),
        ({"backend": "anthropic"}, ModelProviderAnthropicSpec),
        ({"backend": "ollama"}, ModelProviderOllamaSpec),
    ],
)
def test_model_provider_spec_dispatch(data: dict, expected_cls: type) -> None:
    spec = ModelProviderSpec.model_validate(data)
    assert isinstance(spec, expected_cls)


def test_all_specs_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CheckpointerSpec.model_validate({"backend": "in_memory", "bogus": True})


def test_all_specs_frozen() -> None:
    spec = CheckpointerSpec.model_validate({"backend": "postgres", "dsn_env": "X"})
    with pytest.raises((TypeError, ValidationError)):
        spec.dsn_env = "Y"  # type: ignore[misc]


def test_harness_config_default_secret_resolver_is_env_spec() -> None:
    from src.core.harness import HarnessConfig
    from src.core.harness.specs import AgentSpec

    cfg = HarnessConfig(agent=AgentSpec(name="x"))
    assert isinstance(cfg.secret_resolver, SecretResolverEnvSpec)
    assert cfg.checkpointer is None
    assert cfg.memory_store is None
    assert cfg.job_storage is None
    assert cfg.identity_source is None
    assert cfg.model_provider is None


def test_harness_config_routes_to_concrete_checkpointer() -> None:
    from src.core.harness import HarnessConfig
    from src.core.harness.specs import AgentSpec

    cfg = HarnessConfig(
        agent=AgentSpec(name="x"),
        checkpointer={"backend": "postgres", "dsn_env": "CKPT_DSN"},
    )
    assert isinstance(cfg.checkpointer, CheckpointerPostgresSpec)


def test_import_path_field_accepted_on_each_surface() -> None:
    cases = [
        (CheckpointerSpec, {"backend": "in_memory", "import_path": "m:C"}),
        (MemoryStoreSpec, {"backend": "in_memory", "import_path": "m:C"}),
        (JobStorageSpec, {"backend": "json", "import_path": "m:C"}),
        (IdentitySourceSpec, {"backend": "local_fs", "import_path": "m:C"}),
        (SecretResolverSpec, {"backend": "env", "import_path": "m:C"}),
        (ModelProviderSpec, {"backend": "ollama", "import_path": "m:C"}),
    ]
    for spec_cls, data in cases:
        spec = spec_cls.model_validate(data)
        assert spec.import_path == "m:C"
