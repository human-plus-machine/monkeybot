"""Unit tests for knowledge embedding adapters + factory."""

from __future__ import annotations

import math

import pytest

from monkeybot.core.knowledge.embeddings.base import (
    l2_normalize,
    maybe_truncate_and_renorm,
)
from monkeybot.core.knowledge.embeddings.factory import (
    create_embedding_provider,
    provider_defaults,
)
from monkeybot.core.knowledge.embeddings.gemini import GeminiEmbeddingProvider
from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.embeddings.openai_compat import OpenAICompatEmbeddingProvider
from monkeybot.core.knowledge.types import EmbeddingSettings


def test_l2_normalize_unit_length() -> None:
    vec = l2_normalize([3.0, 4.0])
    assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-6
    assert abs(vec[0] - 0.6) < 1e-6
    assert abs(vec[1] - 0.8) < 1e-6


def test_truncate_and_renorm_matryoshka() -> None:
    full = [1.0] * 8
    out = maybe_truncate_and_renorm(full, 4)
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


def test_provider_defaults_table() -> None:
    assert provider_defaults("openai").model == "text-embedding-3-small"
    assert provider_defaults("voyage").api_key_env == "VOYAGE_API_KEY"
    assert provider_defaults("gemini").dimensions == 768
    assert provider_defaults("google").model == "text-embedding-004"
    with pytest.raises(ValueError, match="unknown"):
        provider_defaults("nope")


def test_create_openai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = create_embedding_provider(
        EmbeddingSettings(
            enabled=True,
            provider="openai",
            model="text-embedding-3-small",
            dimensions=1024,
            base_url="https://api.openai.com/v1",
        )
    )
    assert isinstance(provider, OpenAICompatEmbeddingProvider)
    assert provider.model_id == "text-embedding-3-small"
    assert provider.dim == 1024


def test_create_openai_compatible_requires_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="base_url"):
        create_embedding_provider(
            EmbeddingSettings(
                enabled=True,
                provider="openai_compatible",
                model="my-embed",
                dimensions=768,
                base_url="",
            )
        )


def test_create_voyage_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    provider = create_embedding_provider(
        EmbeddingSettings(
            enabled=True,
            provider="voyage",
            model="voyage-3-lite",
            dimensions=512,
            base_url="https://api.voyageai.com/v1",
        )
    )
    assert isinstance(provider, OpenAICompatEmbeddingProvider)
    assert provider.dim == 512


def test_create_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        create_embedding_provider(
            EmbeddingSettings(enabled=True, provider="not-a-real-provider")
        )


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
async def test_openai_embed_passes_dimensions_no_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = create_embedding_provider(
        EmbeddingSettings(
            enabled=True,
            provider="openai",
            model="text-embedding-3-small",
            dimensions=8,
            base_url="https://api.openai.com/v1",
        )
    )
    assert isinstance(provider, OpenAICompatEmbeddingProvider)

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
            return _Resp([_Item(i, [0.25] * 8) for i in range(n)])

    class _Client:
        embeddings = _Embeddings()

    provider._client = _Client()  # type: ignore[attr-defined]

    docs = await provider.embed_documents(["hello"])
    assert len(docs) == 1
    assert len(docs[0]) == 8
    assert captured["kwargs"]["input"] == ["hello"]  # type: ignore[index]
    assert captured["kwargs"]["dimensions"] == 8  # type: ignore[index]
    assert "extra_body" not in captured["kwargs"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_voyage_embed_sets_input_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    provider = create_embedding_provider(
        EmbeddingSettings(
            enabled=True,
            provider="voyage",
            model="voyage-3-lite",
            dimensions=4,
            base_url="https://api.voyageai.com/v1",
        )
    )
    assert isinstance(provider, OpenAICompatEmbeddingProvider)

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
            return _Resp([_Item(i, [0.5] * 4) for i in range(n)])

    class _Client:
        embeddings = _Embeddings()

    provider._client = _Client()  # type: ignore[attr-defined]

    await provider.embed_documents(["doc"])
    assert captured["kwargs"]["extra_body"] == {"input_type": "document"}  # type: ignore[index]

    await provider.embed_query("q")
    assert captured["kwargs"]["extra_body"] == {"input_type": "query"}  # type: ignore[index]


@pytest.mark.asyncio
async def test_gemini_embed_uses_task_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gi-test")
    provider = GeminiEmbeddingProvider(dimensions=4)

    captured: list[dict[str, object]] = []

    class _Emb:
        def __init__(self, values: list[float]) -> None:
            self.values = values

    class _Resp:
        def __init__(self, values: list[float]) -> None:
            self.embeddings = [_Emb(values)]

    class _Models:
        def embed_content(self, **kwargs: object) -> _Resp:
            captured.append(kwargs)
            return _Resp([0.5] * 4)

    class _Client:
        models = _Models()

    provider._client = _Client()  # type: ignore[attr-defined]

    docs = await provider.embed_documents(["hello"])
    assert len(docs) == 1
    assert len(docs[0]) == 4
    assert captured[0]["contents"] == "hello"
    cfg = captured[0]["config"]
    assert getattr(cfg, "task_type") == "RETRIEVAL_DOCUMENT"

    await provider.embed_query("find")
    assert getattr(captured[1]["config"], "task_type") == "RETRIEVAL_QUERY"


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
