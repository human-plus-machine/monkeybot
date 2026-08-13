"""Tests for `monkeybot chat -c/--continue` (auto-resume most recent session)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from monkeybot_cli.commands.chat import (
    _plain_chat_session,
    _resolve_continue_session_id,
    run_chat,
)
from monkeybot_cli.main import build_parser


def _client_with_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("monkeybot_cli.commands.chat.httpx.Client", fake_client)


def test_continue_flag_sets_resume_last() -> None:
    args = build_parser().parse_args(["chat", "--continue"])
    assert args.resume_last is True
    assert args.session is None


def test_continue_short_flag_sets_resume_last() -> None:
    args = build_parser().parse_args(["chat", "-c"])
    assert args.resume_last is True


def test_continue_and_session_flag_coexist() -> None:
    args = build_parser().parse_args(["chat", "--session", "abc", "-c"])
    assert args.session == "abc"
    assert args.resume_last is True


def test_resolve_continue_session_id_returns_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat-history"
        assert request.url.params["limit"] == "1"
        return httpx.Response(
            200,
            json={
                "threads": [
                    {
                        "session_id": "newest-session",
                        "last_message_at": 2,
                        "message_count": 3,
                        "preview": "hi",
                    }
                ]
            },
        )

    _client_with_transport(monkeypatch, handler)
    assert _resolve_continue_session_id("http://127.0.0.1:8080") == "newest-session"


def test_resolve_continue_session_id_empty_history(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"threads": []})

    _client_with_transport(monkeypatch, handler)
    assert _resolve_continue_session_id("http://127.0.0.1:8080") is None


def test_resolve_continue_session_id_http_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    _client_with_transport(monkeypatch, handler)
    assert _resolve_continue_session_id("http://127.0.0.1:8080") is None
    assert "starting a new session" in capsys.readouterr().err


def test_resolve_continue_session_id_malformed_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for PR #179 review: a 2xx response with malformed JSON must
    be swallowed like an HTTP error, not raised — run_chat calls this before
    entering its cleanup try/finally, so an uncaught exception here would
    leave a spawned gateway process and log file behind.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json {{{")

    _client_with_transport(monkeypatch, handler)
    assert _resolve_continue_session_id("http://127.0.0.1:8080") is None
    assert "starting a new session" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        [1, 2, 3],
        "just a string",
        {"threads": "not-a-list"},
        {"threads": ["not-a-dict"]},
        {"threads": None},
    ],
)
def test_resolve_continue_session_id_invalid_threads_shape(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    """A 2xx response with valid JSON but an unexpected shape (non-object body,
    threads not a list, or a non-dict thread entry) must resolve to None
    rather than raising AttributeError/TypeError out of run_chat.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    _client_with_transport(monkeypatch, handler)
    assert _resolve_continue_session_id("http://127.0.0.1:8080") is None


def test_run_chat_continue_resolves_and_sets_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "monkeybot_cli.commands.chat._resolve_continue_session_id",
        lambda base: "resolved-session-id",
    )
    args = build_parser().parse_args(
        ["chat", "--url", "http://127.0.0.1:65001", "--continue"]
    )
    run_chat(args)
    assert args.session == "resolved-session-id"
    assert "Continuing session resolved" in capsys.readouterr().out


def test_bye_prints_continue_hint_in_plain_path(capsys: pytest.CaptureFixture[str]) -> None:
    args = MagicMock(
        model_provider=None,
        model_name=None,
        show_thinking=False,
        verbose=False,
        usage=False,
        session=None,
    )

    async def fake_get(url: str) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url))

    async def fake_post(url: str, json: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(201, request=request, json={"session_id": "sess-1"})

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.post = fake_post
    mock_client.aclose = AsyncMock()

    read_lines = iter(["/bye"])

    async def fake_read_line(prompt: str, interrupt: asyncio.Event, **kwargs: object) -> str | None:
        return next(read_lines)

    with (
        patch("monkeybot_cli.chat_session.httpx.AsyncClient", return_value=mock_client),
        patch("monkeybot_cli.commands.chat._read_line", fake_read_line),
    ):
        code = asyncio.run(
            _plain_chat_session(args, "http://localhost:8080", spawned_gateway=False)
        )

    assert code == 0
    out = capsys.readouterr().out
    assert "Goodbye" in out
    assert "monkeybot chat --continue" in out
