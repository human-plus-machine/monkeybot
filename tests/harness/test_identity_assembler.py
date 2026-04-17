"""E2E test: assembler inserts :class:`IdentityResolutionMW` when configured.

Mirrors the style of ``test_build_universal_agent.py`` but drives the
identity-specific surface: ensures the middleware is wired at the right
pipeline position and that the cache is reachable from the compiled
agent's middleware stack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.core.harness import (
    AgentSpec,
    HarnessConfig,
    IdentitySpec,
    SandboxSpec,
    SecuritySpec,
    build_universal_agent,
)
from src.core.harness.events import Principal
from src.core.harness.extensions.specs.identity_source import IdentitySourceLocalFSSpec
from src.core.harness.extensions.values import LoadedIdentity
from src.core.harness.middleware.identity_resolution import IdentityResolutionMW


def _identity(principal_id: str) -> LoadedIdentity:
    return LoadedIdentity(
        principal_id=principal_id,
        soul="s",
        rules="r",
        identity="i",
        user="u",
        index="ix",
        memory="m",
        heartbeat="h",
        loaded_at=datetime.now(UTC),
        ttl_seconds=60,
        source_backend="callable",
    )


async def _identity_fn(principal: Principal, _: str | None) -> LoadedIdentity:
    return _identity(principal.id)


def _base_cfg(tmp_path: Path, *, identity_spec: bool = True) -> HarnessConfig:
    identity_source = (
        IdentitySourceLocalFSSpec(backend="local_fs", dir=str(tmp_path / "identity"))
        if identity_spec
        else None
    )
    return HarnessConfig(
        agent=AgentSpec(name="t"),
        identity=IdentitySpec(dir=str(tmp_path), enforce_rules=False),
        security=SecuritySpec(principal_required=False),
        sandbox=SandboxSpec(backend="local_shell"),
        identity_source=identity_source,
    )


def test_assembler_inserts_identity_resolution_mw(tmp_path: Path) -> None:
    """Assembler inserts :class:`IdentityResolutionMW` between principal + rules."""
    cfg = _base_cfg(tmp_path)
    compiled = build_universal_agent(cfg)

    mw_names = [type(mw).__name__ for mw in compiled.middleware]
    assert "IdentityResolutionMW" in mw_names
    idx_principal = mw_names.index("PrincipalPropagationMW")
    idx_identity = mw_names.index("IdentityResolutionMW")
    idx_rules = mw_names.index("RulesEnforcementMW")
    assert idx_principal < idx_identity < idx_rules


def test_assembler_skips_identity_resolution_mw_without_spec(tmp_path: Path) -> None:
    """When ``identity_source`` is absent the middleware is NOT inserted."""
    cfg = _base_cfg(tmp_path, identity_spec=False)
    compiled = build_universal_agent(cfg)
    mw_names = [type(mw).__name__ for mw in compiled.middleware]
    assert "IdentityResolutionMW" not in mw_names


def test_middleware_of_identity_resolution_exposes_cache(tmp_path: Path) -> None:
    """The compiled stack surfaces the live cache for admin tooling."""
    cfg = _base_cfg(tmp_path)
    compiled = build_universal_agent(cfg)
    resolver = next(mw for mw in compiled.middleware if isinstance(mw, IdentityResolutionMW))
    stats = resolver.cache.stats()
    assert stats["size"] == 0
    assert "hits" in stats and "misses" in stats
