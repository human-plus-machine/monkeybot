"""Phase 6 integration test — full-stack harness assembly across every surface.

Builds a :class:`HarnessConfig` that opts into every extension surface
shipped by ``harness-extensibility``:

* Story 1 Registry + ABCs
* Story 2 Checkpointer (in-memory ABC backend)
* Story 3 MemoryStore (in-memory ABC backend)
* Story 4 JobStorage (JSON-file — now registered under spec-aligned name
  ``json`` per Phase 6 Item 1)
* Story 5 IdentitySource + IdentityResolutionMW + event bus wiring
  (Phase 6 Item 3 — IDENTITY_* events flow through the bus)
* Story 6 SecretResolver (env)
* Story 7 ModelProvider (OpenAI — boundary wraps API key in SecretStr
  per Phase 6 Item 8)

The assembler is expected to resolve every spec and populate the new
direct handles on :class:`CompiledAgent` (Phase 6 Item 6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness.assembler import build_universal_agent
from src.core.harness.extensions.base import (
    Checkpointer as ExtensionsCheckpointer,
)
from src.core.harness.extensions.base import (
    IdentitySource,
    JobStorage,
    MemoryStore,
    SecretResolver,
)
from src.core.harness.specs import AgentSpec, HarnessConfig, IdentitySpec


@pytest.fixture()
def identity_memory_dir(tmp_path: Path) -> Path:
    """Provision a minimal identity memory directory with the required files."""
    root = tmp_path / "identity" / "default"
    root.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("SOUL.md", "# soul"),
        ("RULES.md", "# rules"),
        ("IDENTITY.md", "# identity"),
        ("USER.md", ""),
        ("INDEX.md", ""),
        ("MEMORY.md", ""),
        ("HEARTBEAT.md", ""),
    ):
        (root / name).write_text(body)
    return tmp_path / "identity"


def _build_full_stack_config(
    tmp_path: Path, identity_dir: Path
) -> HarnessConfig:
    """Build a HarnessConfig that exercises every extension surface."""
    return HarnessConfig(
        agent=AgentSpec(
            name="integration-test-bot",
            model="gpt-4o-mini",
            provider="openai",
            temperature=0.0,
            max_output_tokens=512,
        ),
        identity=IdentitySpec(
            dir=str(identity_dir / "default"),
            enforce_rules=False,
        ),
        checkpointer={"backend": "in_memory"},
        memory_store={"backend": "in_memory"},
        job_storage={"backend": "json", "path": str(tmp_path / "jobs.json")},
        identity_source={
            "backend": "local_fs",
            "dir": str(identity_dir),
            "per_principal_subdir": True,
            "cache_ttl_seconds": 60,
        },
        secret_resolver={"backend": "env"},
        model_provider={"backend": "openai", "api_key_handle": "OPENAI_API_KEY"},
    )


def test_assembler_resolves_every_extension_surface(
    tmp_path: Path, identity_memory_dir: Path
) -> None:
    """Every configured spec resolves and lands on the CompiledAgent."""
    cfg = _build_full_stack_config(tmp_path, identity_memory_dir)

    compiled = build_universal_agent(cfg)

    assert isinstance(compiled.checkpointer_ext, ExtensionsCheckpointer), (
        "ABC-based checkpointer should be resolved from cfg.checkpointer"
    )
    assert isinstance(compiled.memory_store, MemoryStore), (
        "MemoryStore should be resolved from cfg.memory_store"
    )
    assert isinstance(compiled.job_storage, JobStorage), (
        "JobStorage should be resolved from cfg.job_storage (registered as 'json')"
    )
    assert isinstance(compiled.identity_source, IdentitySource), (
        "IdentitySource should be resolved from cfg.identity_source"
    )
    assert isinstance(compiled.secret_resolver, SecretResolver), (
        "SecretResolver should be resolved from cfg.secret_resolver"
    )
    assert compiled.checkpointer is compiled.checkpointer_ext, (
        ".checkpointer property returns the ABC-based instance when available"
    )


def test_identity_resolution_mw_injected_when_identity_source_set(
    tmp_path: Path, identity_memory_dir: Path
) -> None:
    """Opting into identity_source injects IdentityResolutionMW at slot 1."""
    cfg = _build_full_stack_config(tmp_path, identity_memory_dir)

    compiled = build_universal_agent(cfg)

    names = compiled.middleware_names()
    assert "IdentityResolutionMW" in names, (
        f"expected IdentityResolutionMW in pipeline, got {names!r}"
    )
    assert names.index("IdentityResolutionMW") == 1, (
        "IdentityResolutionMW must sit at slot 1 per 1B §4 pipeline order"
    )


def test_identity_mw_has_event_bus_wired(
    tmp_path: Path, identity_memory_dir: Path
) -> None:
    """Phase 6 Item 3: IdentityResolutionMW is handed the shared event bus."""
    cfg = _build_full_stack_config(tmp_path, identity_memory_dir)

    compiled = build_universal_agent(cfg)

    identity_mw = next(
        mw for mw in compiled.middleware if type(mw).__name__ == "IdentityResolutionMW"
    )
    assert identity_mw.event_bus is compiled.event_bus, (
        "IdentityResolutionMW.event_bus must be the shared CompiledAgent.event_bus"
    )
    assert identity_mw.versions is not None


def test_job_storage_builtin_registered_under_spec_name_json() -> None:
    """Phase 6 Item 1: JobStorage.registry resolves a spec with backend='json'."""
    from src.core.harness.extensions import job_storage as _  # noqa: F401

    entry = JobStorage.registry.entry("json")
    assert entry is not None, "builtin 'json' JobStorage backend must be registered"
    assert entry.source == "builtin"

    # Legacy alias preserved for back-compat with Phase 4 consumers.
    legacy = JobStorage.registry.entry("json_file")
    assert legacy is not None, (
        "legacy 'json_file' alias must stay registered for back-compat"
    )
