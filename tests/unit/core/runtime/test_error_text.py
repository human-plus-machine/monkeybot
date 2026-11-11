"""Client error text names the failure kind and never the provider body."""

from __future__ import annotations

import httpx
import pytest
from anthropic import APIStatusError as AnthropicStatusError
from openai import APIStatusError as OpenAIStatusError

from monkeybot.core.runtime.error_text import client_error_text
from monkeybot.core.runtime.preempt import StreamStalledError
from monkeybot.core.runtime.turn_loop import CONTEXT_OVERFLOW_ERROR
from monkeybot.providers._openai_compat import ProviderRateLimitError, ProviderServerError


def _status_error(
    cls: type[BaseException],
    *,
    status: int,
    body: str,
) -> BaseException:
    request = httpx.Request("POST", "https://provider.example/v1/messages")
    response = httpx.Response(status, request=request)
    return cls(body, response=response, body={"error": {"message": body}})


def test_each_failure_kind_uses_a_fixed_sentence() -> None:
    stalled = StreamStalledError(idle_timeout_s=1.0)
    rate = _status_error(OpenAIStatusError, status=429, body="rate body sk-live-rate")
    server = _status_error(AnthropicStatusError, status=529, body="overloaded sk-ant-secret")
    connection = TimeoutError("timed out talking to 10.1.2.3")

    assert client_error_text(stalled, request_id="r1") == (
        "The model provider stopped responding. (ref r1)"
    )
    assert client_error_text(rate, request_id="r1") == (
        "The model provider is rate-limiting requests. Wait a moment and try again. "
        "(HTTP 429, ref r1)"
    )
    assert client_error_text(server, request_id="r1") == (
        "The model provider returned a server error. Try again in a moment. (HTTP 529, ref r1)"
    )
    assert client_error_text(connection, request_id="r1") == (
        "Lost the connection to the model provider. (ref r1)"
    )
    for text in (
        client_error_text(rate, request_id="r1"),
        client_error_text(server, request_id="r1"),
    ):
        assert "sk-live" not in text
        assert "sk-ant" not in text
        assert "body" not in text


def test_auth_and_other_client_errors_drop_the_body() -> None:
    denied = _status_error(AnthropicStatusError, status=401, body="invalid x-api-key sk-ant-xyz")
    rejected = _status_error(OpenAIStatusError, status=400, body="bad schema sk-live-xyz")

    assert client_error_text(denied, request_id="") == (
        "The model provider rejected the credentials. Check the API key. (HTTP 401)"
    )
    assert client_error_text(rejected, request_id="req-9") == (
        "The model provider rejected the request. (HTTP 400, ref req-9)"
    )
    assert "sk-ant" not in client_error_text(denied, request_id="r")
    assert "sk-live" not in client_error_text(rejected, request_id="r")


def test_unknown_failure_names_the_exception_type_only() -> None:
    text = client_error_text(RuntimeError("boom secret"), request_id="r1")
    assert text == "The request failed (RuntimeError). (ref r1)"
    assert "boom" not in text
    assert "secret" not in text


def test_provider_rate_limit_error_passes_through() -> None:
    original = _status_error(OpenAIStatusError, status=429, body="raw quota sk-live-hidden")
    wrapped = ProviderRateLimitError("openai", "gpt", original)
    text = client_error_text(wrapped, request_id="r1")
    assert text == str(wrapped)
    assert "sk-live" not in text
    assert "HTTP 429" not in text


def test_provider_server_error_passes_through() -> None:
    original = _status_error(OpenAIStatusError, status=500, body="raw server sk-live-hidden")
    wrapped = ProviderServerError("openai", "gpt", original)
    assert client_error_text(wrapped, request_id="r1") == str(wrapped)


def test_context_overflow_uses_the_fixed_sentence() -> None:
    exc = RuntimeError("prompt is too long: sk-live-overflow")
    assert client_error_text(exc, request_id="r1") == CONTEXT_OVERFLOW_ERROR
    assert "sk-live" not in client_error_text(exc, request_id="r1")


def test_missing_status_and_request_id_add_no_suffix() -> None:
    assert client_error_text(TimeoutError(), request_id="") == (
        "Lost the connection to the model provider."
    )


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "credentials"), (403, "credentials"), (422, "rejected the request")],
)
def test_client_statuses(status: int, fragment: str) -> None:
    exc = _status_error(OpenAIStatusError, status=status, body="secret-body")
    text = client_error_text(exc, request_id="r1")
    assert fragment in text
    assert f"HTTP {status}" in text
    assert "secret-body" not in text
