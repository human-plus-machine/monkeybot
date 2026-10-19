"""Fireworks provider construction and base URL resolution."""

from __future__ import annotations

import pytest

from monkeybot.providers.fireworks import FireworksProvider


def test_fireworks_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FIREWORKS_API_KEY"):
        FireworksProvider()


def test_fireworks_provider_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test")
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)
    provider = FireworksProvider()
    assert provider._base_url == "https://api.fireworks.ai/inference/v1"


def test_fireworks_provider_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test")
    monkeypatch.setenv("FIREWORKS_BASE_URL", "https://example.internal/v1/")
    provider = FireworksProvider()
    assert provider._base_url == "https://example.internal/v1"
