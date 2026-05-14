"""Integration tests for WebhookGateway."""
from __future__ import annotations

import hashlib
import hmac as _hmac
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from monkeybot.core.events import AssistantDelta, TurnComplete
from monkeybot.gateway.webhook import WebhookGateway, _verify_hmac, load_bot_webhook


def make_mock_loop(response_text: str = "hello") -> MagicMock:
    """Create a mock AgentLoop that yields AssistantDelta then TurnComplete."""
    mock_loop = MagicMock()

    async def fake_run(message: str, session_id: str) -> Any:  # type: ignore[return]
        yield AssistantDelta(text=response_text)
        yield TurnComplete(
            run_id=session_id,
            input_tokens=1,
            output_tokens=1,
            duration_ms=10,
            cost_usd=0.0,
        )

    mock_loop.run = fake_run
    return mock_loop


def make_client(
    extract: Any = None,
    fmt: Any = None,
    response_text: str = "hello",
) -> Any:
    """Create a TestClient for WebhookGateway."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    mock_loop = make_mock_loop(response_text)
    gateway = WebhookGateway(
        loop=mock_loop,
        session_id_fn=lambda p: "test-session",
        extract_message=extract or (lambda p: p.get("text")),
        format_response=fmt,
    )
    app = gateway.build_app()
    return TestClient(app)


def test_health_check() -> None:
    """GET /health returns {"status": "ok"} with HTTP 200."""
    client = make_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_valid_payload() -> None:
    """POST /webhook with valid JSON runs agent and returns response."""
    client = make_client()
    resp = client.post("/webhook", json={"text": "hello bot"})
    assert resp.status_code == 200
    assert resp.json() == {"text": "hello"}


def test_webhook_extract_returns_none() -> None:
    """POST /webhook where extract_message returns None skips agent call."""
    client = make_client(extract=lambda p: None)
    resp = client.post("/webhook", json={"type": "ADDED_TO_SPACE"})
    assert resp.status_code == 200
    assert resp.json() == {"text": ""}


def test_webhook_non_json_body() -> None:
    """POST /webhook with non-JSON body returns 422 or 500."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    mock_loop = make_mock_loop()
    gateway = WebhookGateway(
        loop=mock_loop,
        session_id_fn=lambda p: "s",
        extract_message=lambda p: p.get("text"),
    )
    app = gateway.build_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/webhook",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (422, 500)


def test_webhook_wrong_hmac() -> None:
    """POST /webhook with WEBHOOK_SECRET set and wrong HMAC returns 401."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    mock_loop = make_mock_loop()
    gateway = WebhookGateway(
        loop=mock_loop,
        session_id_fn=lambda p: "s",
        extract_message=lambda p: p.get("text"),
    )
    app = gateway.build_app()
    client = TestClient(app, raise_server_exceptions=False)
    with patch.dict(os.environ, {"WEBHOOK_SECRET": "mysecret"}):
        resp = client.post(
            "/webhook",
            json={"text": "hi"},
            headers={"X-Hub-Signature-256": "sha256=wrongsig"},
        )
    assert resp.status_code == 401


def test_webhook_correct_hmac() -> None:
    """POST /webhook with correct HMAC returns 200."""
    import json as _json  # noqa: PLC0415

    from fastapi.testclient import TestClient  # noqa: PLC0415

    mock_loop = make_mock_loop()
    gateway = WebhookGateway(
        loop=mock_loop,
        session_id_fn=lambda p: "s",
        extract_message=lambda p: p.get("text"),
    )
    app = gateway.build_app()
    client = TestClient(app, raise_server_exceptions=False)
    secret = "mysecret"
    body = _json.dumps({"text": "hi"}).encode()
    sig = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with patch.dict(os.environ, {"WEBHOOK_SECRET": secret}):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
        )
    assert resp.status_code == 200


def test_load_bot_webhook_with_file(tmp_path: Path) -> None:
    """load_bot_webhook returns callables when webhook.py is present."""
    webhook_py = tmp_path / "webhook.py"
    webhook_py.write_text(
        "def extract_message(p): return p.get('text')\n"
        "def format_response(t): return {'text': t}\n"
        "def session_id(p): return 'sid'\n"
    )
    extract, fmt, sid = load_bot_webhook(str(tmp_path))
    assert extract({"text": "hi"}) == "hi"
    assert fmt("hello") == {"text": "hello"}
    assert sid({}) == "sid"


def test_load_bot_webhook_no_file(tmp_path: Path) -> None:
    """load_bot_webhook returns generic fallbacks when webhook.py absent."""
    extract, fmt, sid = load_bot_webhook(str(tmp_path))
    assert extract({"text": "hi"}) == "hi"
    assert fmt("hello") == {"text": "hello"}
    assert isinstance(sid({}), str)  # ULID


def test_load_bot_webhook_syntax_error(tmp_path: Path) -> None:
    """load_bot_webhook raises ImportError with file path for syntax errors."""
    bad_py = tmp_path / "webhook.py"
    bad_py.write_text("def broken(\n")  # syntax error
    with pytest.raises(ImportError, match=str(tmp_path)):
        load_bot_webhook(str(tmp_path))


def test_verify_hmac_correct() -> None:
    """_verify_hmac returns True for correct sha256= signature."""
    secret = "test"
    body = b"hello"
    sig = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert _verify_hmac(secret, body, sig) is True


def test_verify_hmac_bare_hex() -> None:
    """_verify_hmac accepts bare hex digest without sha256= prefix."""
    secret = "test"
    body = b"hello"
    sig = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert _verify_hmac(secret, body, sig) is True


def test_verify_hmac_wrong() -> None:
    """_verify_hmac returns False for wrong signature."""
    assert _verify_hmac("test", b"hello", "sha256=wrongsig") is False


def test_verify_hmac_none() -> None:
    """_verify_hmac returns False when header is None."""
    assert _verify_hmac("test", b"hello", None) is False


def test_gchat_extract_message() -> None:
    """Google Chat MESSAGE event returns message.text."""
    extract, _, _ = load_bot_webhook("bots/example-bot")
    result = extract({"type": "MESSAGE", "message": {"text": "hello"}})
    assert result == "hello"


def test_gchat_added_to_space_returns_none() -> None:
    """Google Chat ADDED_TO_SPACE returns None."""
    extract, _, _ = load_bot_webhook("bots/example-bot")
    result = extract({"type": "ADDED_TO_SPACE"})
    assert result is None


def _load_slack_module() -> object:
    """Load webhook_slack_example.py via importlib."""
    import importlib.util as _ilu  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    slack_path = _Path("bots/example-bot/webhook_slack_example.py")
    spec = _ilu.spec_from_file_location("_slack", slack_path)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_slack_extract_message() -> None:
    """Slack message event returns event.text."""
    mod = _load_slack_module()
    result = mod.extract_message({"event": {"text": "hello from slack"}})  # type: ignore[union-attr]
    assert result == "hello from slack"


def test_slack_bot_message_returns_none() -> None:
    """Slack bot_message subtype returns None."""
    mod = _load_slack_module()
    result = mod.extract_message({"event": {"subtype": "bot_message", "text": "bot says hi"}})  # type: ignore[union-attr]
    assert result is None
