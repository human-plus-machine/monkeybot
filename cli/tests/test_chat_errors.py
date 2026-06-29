"""Tests for chat CLI HTTP and stream error handling."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from monkeybot_cli.commands.chat import (
    _chat_session,
    _format_gateway_log_tail,
    _format_http_error,
    _SpawnedGateway,
    _tail_gateway_log,
    run_chat,
)


def test_format_http_error_status() -> None:
    request = httpx.Request("POST", "http://localhost:8080/sessions")
    response = httpx.Response(500, request=request, text="internal error")
    exc = httpx.HTTPStatusError("server error", request=request, response=response)

    message = _format_http_error("Session creation", exc)

    assert message == "Session creation failed: server error"


def test_format_http_error_transport() -> None:
    request = httpx.Request("GET", "http://localhost:8080/health")
    exc = httpx.ConnectError("connection refused", request=request)

    message = _format_http_error("Gateway health check", exc)

    assert "Gateway health check failed" in message
    assert "connection refused" in message


def test_chat_session_create_failure_returns_readable_error(capsys: pytest.CaptureFixture[str]) -> None:
    args = MagicMock(model_provider=None, model_name=None)

    async def fake_get(url: str) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url))

    async def fake_post(url: str, json: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(503, request=request, text="service unavailable")

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.post = fake_post
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("monkeybot_cli.commands.chat.httpx.AsyncClient", return_value=mock_client):
        code = asyncio.run(_chat_session(args, "http://localhost:8080", spawned_gateway=False))

    assert code == 1
    err = capsys.readouterr().err
    assert "Session creation failed" in err
    assert "503" in err


def test_chat_session_stream_failure_exits_turn(capsys: pytest.CaptureFixture[str]) -> None:
    args = MagicMock(
        model_provider=None,
        model_name=None,
        show_thinking=False,
        verbose=False,
        usage=False,
    )

    async def fake_get(url: str) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url))

    async def fake_post(url: str, json: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=json)
        if url.endswith("/reply"):
            return httpx.Response(200, request=request, json={"accepted": True})
        return httpx.Response(201, request=request, json={"session_id": "sess-1"})

    class FailingStream:
        async def __aenter__(self) -> FailingStream:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "stream failed",
                request=httpx.Request("GET", "http://localhost:8080/sessions/sess-1/events"),
                response=httpx.Response(
                    500,
                    request=httpx.Request("GET", "http://localhost:8080/sessions/sess-1/events"),
                ),
            )

    mock_client = AsyncMock()
    mock_client.get = fake_get
    mock_client.post = fake_post
    mock_client.stream = MagicMock(return_value=FailingStream())
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    read_lines = iter(["hello", None])

    async def fake_read_line(prompt: str, interrupt: asyncio.Event, **kwargs: object) -> str | None:
        return next(read_lines)

    with (
        patch("monkeybot_cli.commands.chat.httpx.AsyncClient", return_value=mock_client),
        patch("monkeybot_cli.commands.chat._read_line", fake_read_line),
        patch("monkeybot_cli.commands.chat._print_welcome"),
    ):
        code = asyncio.run(_chat_session(args, "http://localhost:8080", spawned_gateway=False))

    assert code == 0
    err = capsys.readouterr().err
    assert "Event stream failed" in err


def test_format_gateway_log_tail_limits_lines(tmp_path) -> None:
    log_path = tmp_path / "gateway.log"
    log_path.write_text("\n".join(f"line-{i}" for i in range(50)), encoding="utf-8")

    tail = _format_gateway_log_tail(log_path, max_lines=5)

    assert tail == "\n".join(f"line-{i}" for i in range(45, 50))


def test_tail_gateway_log_reads_captured_stderr(tmp_path) -> None:
    log_path = tmp_path / "gateway.log"
    log_file = log_path.open("w+", encoding="utf-8")
    log_file.write("ModuleNotFoundError: no module named 'monkeybot'\n")
    log_file.flush()
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.wait.return_value = 1
    gateway = _SpawnedGateway(proc=proc, log_path=log_path, log_file=log_file)

    tail = _tail_gateway_log(gateway)

    assert "ModuleNotFoundError" in tail
    assert log_file.closed


def test_run_chat_prints_gateway_log_on_startup_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: gemini\n  name: test\nruntime:\n  port: 18080\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "gateway.log"
    log_path.write_text("ImportError: gateway bootstrap failed\n", encoding="utf-8")
    log_file = log_path.open("r+", encoding="utf-8")
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.wait.return_value = 1
    spawned = _SpawnedGateway(proc=proc, log_path=log_path, log_file=log_file)
    args = argparse.Namespace(
        cwd=str(tmp_path),
        config=None,
        attach=False,
        url=None,
        port=None,
        model_provider=None,
        model_name=None,
        show_thinking=False,
        verbose=False,
        usage=False,
    )

    with (
        patch("monkeybot_cli.commands.chat.resolve_config", return_value=cfg_dir / "monkeybot.yaml"),
        patch("monkeybot_cli.commands.chat.load_agent_dotenv", return_value=None),
        patch("monkeybot_cli.commands.chat._spawn_gateway", return_value=spawned),
        patch("monkeybot_cli.commands.chat._wait_for_health", return_value=False),
    ):
        code = run_chat(args)

    assert code == 1
    err = capsys.readouterr().err
    assert "Gateway failed to start." in err
    assert "gateway bootstrap failed" in err
    assert "Run `monkeybot run` to see logs." not in err
