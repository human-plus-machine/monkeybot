"""Tests for KnowledgeSubsystem.create degraded-embeddings wiring (H2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.knowledge.subsystem import KnowledgeSubsystem
from monkeybot.core.knowledge.types import EmbeddingSettings, KnowledgeSettings


@pytest.mark.asyncio
async def test_create_surfaces_degraded_reason_on_missing_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requesting embeddings without NVIDIA_API_KEY must set a degraded reason."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge_root = tmp_path / ".monkeybot" / "knowledge"

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge_root),
        index_path=str(knowledge_root / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
        embeddings=EmbeddingSettings(enabled=True),
    )
    subsystem = await KnowledgeSubsystem.create(
        workspace_root=ws,
        settings=settings,
        knowledge_root=knowledge_root,
        index_path=Path(settings.index_path),
    )
    try:
        assert subsystem.embeddings_enabled is False
        assert subsystem._embeddings_degraded_reason is not None  # noqa: SLF001
        assert "NVIDIA_API_KEY" in subsystem._embeddings_degraded_reason  # noqa: SLF001
    finally:
        await subsystem.close()


@pytest.mark.asyncio
async def test_create_surfaces_degraded_reason_on_unknown_provider(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge_root = tmp_path / ".monkeybot" / "knowledge"

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge_root),
        index_path=str(knowledge_root / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
        embeddings=EmbeddingSettings(enabled=True, provider="not-a-real-provider"),
    )
    subsystem = await KnowledgeSubsystem.create(
        workspace_root=ws,
        settings=settings,
        knowledge_root=knowledge_root,
        index_path=Path(settings.index_path),
    )
    try:
        assert subsystem.embeddings_enabled is False
        assert "not-a-real-provider" in (
            subsystem._embeddings_degraded_reason or ""  # noqa: SLF001
        )
    finally:
        await subsystem.close()


@pytest.mark.asyncio
async def test_create_no_degraded_reason_when_embeddings_not_requested(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge_root = tmp_path / ".monkeybot" / "knowledge"

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge_root),
        index_path=str(knowledge_root / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
    )
    subsystem = await KnowledgeSubsystem.create(
        workspace_root=ws,
        settings=settings,
        knowledge_root=knowledge_root,
        index_path=Path(settings.index_path),
    )
    try:
        assert subsystem.embeddings_enabled is False
        assert subsystem._embeddings_degraded_reason is None  # noqa: SLF001
    finally:
        await subsystem.close()
