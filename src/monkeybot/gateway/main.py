"""
Gateway server entrypoint (SSE harness).

Run locally with:

    python -m monkeybot.gateway.main
"""

from __future__ import annotations

import logging
import os

from monkeybot.core.logging_utils import normalize_log_level
from monkeybot.gateway.bootstrap import ensure_gateway_runtime_env, log_gateway_startup

from monkeybot.gateway.sse.app import app

__all__ = ["app"]


def _configure_runtime() -> None:
    """Load the selected agent only when this module is executed as an entrypoint."""
    layout = ensure_gateway_runtime_env()
    logging.basicConfig(
        level=normalize_log_level(os.getenv("LOG_LEVEL")),
        format="%(levelname)s:%(name)s:%(message)s",
    )
    log_gateway_startup(layout)
    for noisy in ("httpx", "httpcore", "urllib3", "google_genai", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    import uvicorn

    _configure_runtime()
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
