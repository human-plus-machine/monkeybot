"""WebhookGateway — platform-agnostic HTTP webhook endpoint for MonkeyBot agents."""
from __future__ import annotations

import hashlib
import hmac as _hmac
import importlib.util
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ulid

from monkeybot.core.events import AssistantDelta
from monkeybot.core.loop import AgentLoop

_log = logging.getLogger(__name__)

MessageExtractor = Callable[[dict[str, Any]], str | None]
ResponseFormatter = Callable[[str], dict[str, Any]]
SessionIdFn = Callable[[dict[str, Any]], str]


def _generic_extract(payload: dict[str, Any]) -> str | None:
    """Generic message extractor — tries common payload shapes."""
    return payload.get("text") or payload.get("message") or payload.get("content")


def _generic_format(text: str) -> dict[str, Any]:
    """Generic response formatter — returns simple text dict."""
    return {"text": text}


def _generic_session_id(payload: dict[str, Any]) -> str:
    """Generic session ID extractor — falls back to a new ULID."""
    return str(payload.get("session_id") or payload.get("user") or ulid.new())


def _verify_hmac(secret: str, body: bytes, header: str | None) -> bool:
    """Verify HMAC-SHA256 signature. Accepts sha256=<hex> or bare <hex>."""
    if not header:
        return False
    expected_hex = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    expected_full = "sha256=" + expected_hex
    return _hmac.compare_digest(expected_full, header) or _hmac.compare_digest(expected_hex, header)


class WebhookGateway:
    """Platform-agnostic HTTP webhook gateway for MonkeyBot agents.

    Accepts POST /webhook from any chat platform. The caller provides
    platform-specific extractor and formatter callables.
    """

    def __init__(
        self,
        loop: AgentLoop,
        session_id_fn: SessionIdFn,
        extract_message: MessageExtractor,
        format_response: ResponseFormatter | None = None,
    ) -> None:
        """Initialize the gateway.

        Args:
            loop: AgentLoop to run agent turns.
            session_id_fn: Callable that extracts a session ID from the payload.
            extract_message: Callable that extracts the user message from the payload.
                Returns None to skip processing (e.g. non-message events).
            format_response: Callable that formats the agent response for the platform.
                Defaults to returning {"text": response}.
        """
        self._loop = loop
        self._session_id_fn = session_id_fn
        self._extract = extract_message
        self._format = format_response or _generic_format

    def build_app(self) -> Any:  # Returns FastAPI — lazy import avoids module-level dep
        """Build and return a FastAPI application with webhook and health endpoints.

        Returns a new FastAPI instance on each call.
        """
        from fastapi import FastAPI, HTTPException, Request  # noqa: PLC0415
        from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415

        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )
        gateway = self  # capture for closure

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        async def webhook_handler(request: Request) -> dict[str, Any]:
            body = await request.body()
            if len(body) > 64 * 1024:
                raise HTTPException(status_code=413, detail="Payload too large")

            secret = os.getenv("WEBHOOK_SECRET", "")
            if secret:
                sig_header = request.headers.get("X-Hub-Signature-256") or request.headers.get(
                    "Authorization", ""
                ).removeprefix("Bearer ")
                if not _verify_hmac(secret, body, sig_header or None):
                    raise HTTPException(status_code=401, detail="Invalid signature")

            payload: dict[str, Any] = json.loads(body)
            user_message = gateway._extract(payload)
            if not user_message:
                return gateway._format("")

            session_id = gateway._session_id_fn(payload)
            parts: list[str] = []
            async for event in gateway._loop.run(user_message, session_id):
                if isinstance(event, AssistantDelta):
                    parts.append(event.text)
            return gateway._format("".join(parts))

        # Resolve annotations explicitly: `from __future__ import annotations` makes all
        # annotations strings; FastAPI resolves them against the module globals, but
        # `Request` is only in the local scope of build_app(). Set to the actual class.
        webhook_handler.__annotations__["request"] = Request

        app.add_api_route("/webhook", webhook_handler, methods=["POST"])

        return app


def load_bot_webhook(
    bot_dir: str,
) -> tuple[MessageExtractor, ResponseFormatter, SessionIdFn]:
    """Load extract_message, format_response, session_id from {bot_dir}/webhook.py.

    Falls back to generic implementations if webhook.py is absent.

    Args:
        bot_dir: Path to the bot directory containing webhook.py.

    Returns:
        Tuple of (extract_message, format_response, session_id) callables.

    Raises:
        ImportError: If webhook.py exists but fails to load (syntax error, etc.).
    """
    webhook_path = Path(bot_dir) / "webhook.py"
    if not webhook_path.exists():
        return _generic_extract, _generic_format, _generic_session_id

    try:
        spec = importlib.util.spec_from_file_location("_bot_webhook", webhook_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create module spec for {webhook_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ImportError(f"Failed to load {webhook_path}: {exc}") from exc

    return module.extract_message, module.format_response, module.session_id
