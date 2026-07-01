"""NVIDIA provider construction and base URL resolution."""

from __future__ import annotations

import pytest

from monkeybot.providers.nvidia import NvidiaProvider


def test_nvidia_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        NvidiaProvider()


def test_nvidia_provider_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.delenv("NVIDIA_BASE_URL", raising=False)
    provider = NvidiaProvider()
    assert provider._base_url == "https://integrate.api.nvidia.com/v1"


def test_nvidia_provider_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.internal/v1/")
    provider = NvidiaProvider()
    assert provider._base_url == "https://example.internal/v1"


def test_nvidia_stores_cache_enabled_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    provider = NvidiaProvider()
    assert provider._cache_enabled is True


def test_nvidia_stores_cache_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    provider = NvidiaProvider(cache_enabled=False)
    assert provider._cache_enabled is False
