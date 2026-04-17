"""E2E test for the AWS enterprise reference example (Story 8).

Loads ``examples/aws-enterprise-agent/harness.yaml``, substitutes ``${VAR}``
placeholders from the process environment, builds the resulting
:class:`HarnessConfig` via :func:`build_universal_agent`, and asserts every
pillar the CompiledAgent currently exposes is wired to the shipped AWS
reference class.

Some pillars are not yet reachable from :class:`CompiledAgent` (memory store
and job storage are not wired through the assembler; ``cfg.checkpointer`` is
ignored in favour of ``scheduler.storage``) — those assertions are marked
with explicit Phase 6 TODOs rather than silently skipped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")

from src.core.harness import HarnessConfig, build_universal_agent  # noqa: E402

EXAMPLE_ROOT = (
    Path(__file__).resolve().parents[2] / "examples" / "aws-enterprise-agent"
)
HARNESS_YAML = EXAMPLE_ROOT / "harness.yaml"


def _load_example_config(tmp_memory_dir: Path) -> HarnessConfig:
    """Return ``HarnessConfig`` parsed from the example YAML with env-subst applied."""
    raw = HARNESS_YAML.read_text()
    expanded = os.path.expandvars(raw)
    data = yaml.safe_load(expanded)
    assert isinstance(data, dict)
    data.setdefault("identity", {})["dir"] = str(tmp_memory_dir)
    return HarnessConfig.from_mapping(data)


def _bootstrap_identity(base: Path) -> None:
    """Materialise the seven memory files the identity loader requires.

    We copy starter content from the example's ``data/memory/`` so the
    loader's strict mode passes.
    """
    src = EXAMPLE_ROOT / "data" / "memory"
    base.mkdir(parents=True, exist_ok=True)
    for name in (
        "SOUL.md",
        "RULES.md",
        "IDENTITY.md",
        "USER.md",
        "INDEX.md",
        "MEMORY.md",
        "HEARTBEAT.md",
    ):
        (base / name).write_text((src / name).read_text())


@pytest.fixture()
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate every placeholder used in ``harness.yaml`` with deterministic values."""
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("KMS_KEY_ID", "alias/test-cmk")
    monkeypatch.setenv("CKPT_DSN", "postgresql://user:pass@localhost:5432/emonk")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def test_harness_yaml_parses_as_v1_config(
    _stub_env: None,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Sanity: the shipped YAML is a valid ``HarnessConfig`` after env substitution."""
    memory_dir = Path(str(tmp_path)) / "memory"
    _bootstrap_identity(memory_dir)
    cfg = _load_example_config(memory_dir)

    assert cfg.agent.name == "aws-enterprise-agent"
    assert cfg.agent.provider == "bedrock"
    assert cfg.checkpointer is not None
    assert cfg.checkpointer.backend == "postgres"
    assert cfg.memory_store is not None
    assert cfg.memory_store.backend == "s3"
    assert cfg.memory_store.bucket == "test-bucket"
    assert cfg.memory_store.kms_key_id == "alias/test-cmk"
    assert cfg.job_storage is not None
    assert cfg.job_storage.backend == "postgres"
    assert cfg.identity_source is not None
    assert cfg.identity_source.backend == "s3"
    assert cfg.identity_source.bucket == "test-bucket"
    assert cfg.secret_resolver is not None
    assert cfg.secret_resolver.backend == "composite"
    assert cfg.model_provider is not None
    assert cfg.model_provider.backend == "bedrock"
    assert cfg.observability.run_package.writer == "s3"
    assert cfg.observability.run_package.sink_uri == "s3://test-bucket/runpackages/"


def test_build_universal_agent_wires_aws_pillars(
    _stub_env: None,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Build the agent and assert every pillar reachable from CompiledAgent."""
    memory_dir = Path(str(tmp_path)) / "memory"
    _bootstrap_identity(memory_dir)
    cfg = _load_example_config(memory_dir)

    compiled = build_universal_agent(cfg)

    assert compiled.identity_source is not None
    assert type(compiled.identity_source).__name__ == "S3IdentitySource"

    assert compiled.secret_resolver is not None
    resolver: Any = compiled.secret_resolver
    inner = getattr(resolver, "inner", resolver)
    assert type(inner).__name__ == "CompositeSecretResolver"

    assert compiled.model_provider is not None
    assert type(compiled.model_provider).__name__ == "BedrockProvider"

    assert type(compiled.run_package_writer).__name__ == "S3RunPackageWriter"

    # TODO(Phase 6): assembler does not yet resolve cfg.checkpointer — it still
    # picks from scheduler.storage. Once Phase 6 reconciles the two, replace
    # the skip below with:
    #     assert type(compiled.session_registry.checkpointer).__name__ == "PostgresCheckpointer"
    assert compiled.session_registry.checkpointer is not None

    # TODO(Phase 6): CompiledAgent does not yet expose memory_store or
    # job_storage; cfg.memory_store and cfg.job_storage are parsed but not
    # wired through the assembler. Once Phase 6 lands, assert:
    #     assert type(compiled.memory_store).__name__ == "S3MemoryStore"
    #     assert type(compiled.job_storage).__name__ == "PostgresJobStorage"
    assert not hasattr(compiled, "memory_store") or compiled.memory_store is None
    assert not hasattr(compiled, "job_storage") or compiled.job_storage is None
