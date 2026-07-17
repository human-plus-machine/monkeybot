"""Unit tests for NVIDIA embedding adapter helpers and factory."""

from __future__ import annotations

import math

import pytest

from monkeybot.core.knowledge.embeddings import create_embedding_provider
from monkeybot.core.knowledge.embeddings.nvidia import (
    NvidiaEmbeddingProvider,
    _l2_normalize,
    _maybe_truncate_and_renorm,
)
from monkeybot.core.knowledge.types import EmbeddingSettings


def test_l2_normalize_unit_length() -> None:
    vec = _l2_normalize([3.0, 4.0])
    assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-6
    assert abs(vec[0] - 0.6) < 1e-6
    assert abs(vec[1] - 0.8) < 1e-6


def test_truncate_and_renorm_matryoshka() -> None:
    full = [1.0] * 8
    out = _maybe_truncate_and_renorm(full, 4)
    assert len(out) == 4
    assert abs(math.sqrt(sum(x * x for x in out)) - 1.0) < 1e-6


def test_nvidia_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        NvidiaEmbeddingProvider()


def test_nvidia_provider_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    provider = NvidiaEmbeddingProvider()
    assert provider.model_id == "nvidia/nemotron-3-embed-1b"
    assert provider.dim == 1024


def test_create_embedding_provider_disabled() -> None:
    assert create_embedding_provider(EmbeddingSettings(enabled=False)) is None


def test_create_embedding_provider_nvidia(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    provider = create_embedding_provider(
        EmbeddingSettings(enabled=True, provider="nvidia", model="nvidia/nemotron-3-embed-1b")
    )
    assert provider is not None
    assert provider.model_id == "nvidia/nemotron-3-embed-1b"


@pytest.mark.asyncio
async def test_nvidia_embed_prefixes_and_client_matryoshka(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API returns native 2048; we truncate+renorm locally (no dimensions kwarg)."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    provider = NvidiaEmbeddingProvider(dimensions=4)

    captured: dict[str, object] = {}

    class _Item:
        def __init__(self, index: int, embedding: list[float]) -> None:
            self.index = index
            self.embedding = embedding

    class _Resp:
        def __init__(self, data: list[_Item]) -> None:
            self.data = data

    class _Embeddings:
        async def create(self, **kwargs: object) -> _Resp:
            captured["kwargs"] = kwargs
            n = len(kwargs["input"])  # type: ignore[arg-type]
            # Simulate integrate.api.nvidia.com: always 2048, no dimensions param.
            full = [0.5] * 2048
            return _Resp([_Item(i, full) for i in range(n)])

    class _Client:
        embeddings = _Embeddings()

    provider._client = _Client()  # type: ignore[attr-defined]

    docs = await provider.embed_documents(["hello", "world"])
    assert len(docs) == 2
    assert all(len(v) == 4 for v in docs)
    assert all(abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6 for v in docs)
    assert "dimensions" not in captured["kwargs"]  # type: ignore[operator]
    assert captured["kwargs"]["input"] == ["passage: hello", "passage: world"]  # type: ignore[index]

    q = await provider.embed_query("find auth")
    assert len(q) == 4
    assert captured["kwargs"]["input"] == ["query: find auth"]  # type: ignore[index]
    assert "dimensions" not in captured["kwargs"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_nvidia_embed_sanitizes_data_image_uris(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline data:image payloads must not reach the API (VLM 503 otherwise)."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    provider = NvidiaEmbeddingProvider(dimensions=4)

    captured: dict[str, object] = {}

    class _Item:
        def __init__(self, index: int, embedding: list[float]) -> None:
            self.index = index
            self.embedding = embedding

    class _Resp:
        def __init__(self, data: list[_Item]) -> None:
            self.data = data

    class _Embeddings:
        async def create(self, **kwargs: object) -> _Resp:
            captured["kwargs"] = kwargs
            n = len(kwargs["input"])  # type: ignore[arg-type]
            return _Resp([_Item(i, [0.5] * 2048) for i in range(n)])

    class _Client:
        embeddings = _Embeddings()

    provider._client = _Client()  # type: ignore[attr-defined]

    poisoned = 'bg: url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB")'
    await provider.embed_documents([poisoned])
    sent = captured["kwargs"]["input"][0]  # type: ignore[index]
    assert "data:image" not in sent
    assert "embedded-media omitted" in sent
    assert sent.startswith("passage: ")


def test_sanitize_embed_text_strips_data_uris() -> None:
    from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text

    raw = 'const x = "data:image/svg+xml,%3Csvg%3E"; // keep'
    out = sanitize_embed_text(raw)
    assert "data:image" not in out
    assert "embedded-media omitted" in out
    assert "// keep" in out
