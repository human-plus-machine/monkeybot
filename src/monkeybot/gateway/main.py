"""
Gateway server entrypoint (SSE harness).

Run locally with:

    python -m monkeybot.gateway.main
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from monkeybot.gateway.sse.app import app  # noqa: E402

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("GATEWAY_PORT", "8000")))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    timeout_graceful_shutdown = int(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT_SEC", "5"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level=log_level,
        access_log=True,
        timeout_graceful_shutdown=timeout_graceful_shutdown,
    )
