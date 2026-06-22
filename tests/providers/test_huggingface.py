"""HuggingFace provider URL resolution and tool-error detection."""

from __future__ import annotations

import pytest

from monkeybot.providers.huggingface import HuggingFaceProvider, _is_tool_unsupported_error


def test_resolve_base_url_default_router() -> None:
    p = HuggingFaceProvider.__new__(HuggingFaceProvider)
    p._endpoint_url = ""
    p._host = "https://router.huggingface.co/hf-inference"
    assert p._resolve_base_url("any-model") == "https://router.huggingface.co/hf-inference/v1"


def test_resolve_base_url_with_v1_suffix() -> None:
    p = HuggingFaceProvider.__new__(HuggingFaceProvider)
    p._endpoint_url = ""
    p._host = "https://router.huggingface.co/cerebras/v1"
    assert p._resolve_base_url("m") == "https://router.huggingface.co/cerebras/v1"


def test_resolve_base_url_dedicated_endpoint() -> None:
    p = HuggingFaceProvider.__new__(HuggingFaceProvider)
    p._endpoint_url = "https://my-endpoint.example/v1"
    p._host = "https://ignored.example"
    assert p._resolve_base_url("m") == "https://my-endpoint.example/v1"


def test_huggingface_provider_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(ValueError, match="HF_TOKEN"):
        HuggingFaceProvider()


def test_huggingface_stores_cache_enabled_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test")
    provider = HuggingFaceProvider()
    assert provider._cache_enabled is True


def test_huggingface_stores_cache_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test")
    provider = HuggingFaceProvider(cache_enabled=False)
    assert provider._cache_enabled is False


def test_is_tool_unsupported_error_openai_status() -> None:
    try:
        import httpx
        from openai import APIStatusError
    except ImportError:
        pytest.skip("openai not installed")

    def _status_error(status: int, message: str, body: object) -> APIStatusError:
        request = httpx.Request("POST", "https://router.huggingface.co/v1/chat/completions")
        response = httpx.Response(status, request=request)
        return APIStatusError(message, response=response, body=body)

    exc = _status_error(
        400,
        "tools not supported",
        {"error": "tools not supported for this model"},
    )
    assert _is_tool_unsupported_error(exc) is True

    auth_exc = _status_error(401, "unauthorized", {"error": "invalid token"})
    assert _is_tool_unsupported_error(auth_exc) is False


def test_is_tool_unsupported_error_generic_message() -> None:
    assert _is_tool_unsupported_error(RuntimeError("tool use unsupported on this model")) is True
    assert _is_tool_unsupported_error(RuntimeError("rate limit exceeded")) is False
