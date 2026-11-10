"""Size-rejection classification, one row per provider phrase."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.context_overflow import OVERFLOW_MESSAGE_PATTERNS, is_context_overflow
from monkeybot.core.types.interfaces import LLMError
from monkeybot.providers.request_budget import RequestByteBudgetError


class _ApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | int | None = None,
        body: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status
        self.code = code
        self.body = body


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _ResponseError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.response = _Response(status)


@pytest.mark.parametrize("pattern", OVERFLOW_MESSAGE_PATTERNS)
def test_each_provider_phrase_matches_a_client_error(pattern: str) -> None:
    assert is_context_overflow(_ApiError(f"provider said: {pattern}", status=400))


@pytest.mark.parametrize("status", [400, 413, None])
def test_size_phrase_matches_at_400_413_or_no_status(status: int | None) -> None:
    assert is_context_overflow(_ApiError("prompt is too long: 9 > 8", status=status))


def test_byte_budget_error_matches() -> None:
    assert is_context_overflow(RequestByteBudgetError("openai request exceeds configured byte cap"))


def test_openai_error_code_matches_without_the_phrase() -> None:
    error = _ApiError(
        "bad request",
        status=400,
        body={"error": {"code": "context_length_exceeded", "message": "too big"}},
    )
    assert is_context_overflow(error)


def test_rate_limit_does_not_match_even_when_it_mentions_tokens() -> None:
    assert not is_context_overflow(
        _ApiError("too many tokens: rate limit", status=429, code="context_length_exceeded")
    )


def test_server_error_does_not_match() -> None:
    assert not is_context_overflow(_ApiError("prompt is too long", status=500))


def test_unrelated_client_error_does_not_match() -> None:
    assert not is_context_overflow(_ApiError("invalid api key", status=400))


def test_payload_too_large_without_a_size_phrase_does_not_match() -> None:
    assert not is_context_overflow(_ApiError("payload too large", status=413))


def test_status_on_the_response_is_read() -> None:
    assert is_context_overflow(_ResponseError("input is too long for requested model", 400))
    assert not is_context_overflow(_ResponseError("input is too long for requested model", 429))


def test_gemini_wrapper_matches_through_the_cause() -> None:
    inner = _ApiError("input token count exceeds the maximum number of tokens allowed", status=400)
    outer = LLMError(str(inner))
    outer.__cause__ = inner
    assert is_context_overflow(outer)


def test_openai_compat_wrapper_matches_through_original() -> None:
    inner = _ApiError("maximum context length is 128000", status=400)
    outer = _ApiError("please try again")
    outer.original = inner  # type: ignore[attr-defined]
    assert is_context_overflow(outer)


def test_context_cause_matches_when_it_is_not_suppressed() -> None:
    inner = _ApiError("context length exceeded")
    outer = _ApiError("outer")
    outer.__context__ = inner
    assert is_context_overflow(outer)
