"""Standalone scheduler worker (HTTP client to a running gateway)."""

from __future__ import annotations

import asyncio
import logging
import os

from monkeybot.core.config.settings import auto_schema_enabled_from_config
from monkeybot.core.persistence.backends import StorageBackend, create_storage_backend
from monkeybot.gateway.bootstrap import ensure_gateway_runtime_env
from monkeybot.scheduler.engine import (
    resolve_scheduler_worker_id,
    run_scheduler_loop,
    scheduler_settings,
)
from monkeybot.scheduler.http_invoker import HttpTickInvoker

logger = logging.getLogger(__name__)


class _HttpSessionBusyChecker:
    def __init__(self, invoker: HttpTickInvoker) -> None:
        self._invoker = invoker

    def is_busy(self, session_id: str) -> bool:
        del session_id, self._invoker
        return False


class _NoopSessionEnsurer:
    async def ensure_session(self, session_id: str) -> None:
        del session_id


def _gateway_base_url() -> str:
    port = os.environ.get("PORT", os.environ.get("GATEWAY_PORT", "8080"))
    host = os.environ.get("MONKEYBOT_GATEWAY_HOST", "127.0.0.1")
    explicit = os.environ.get("MONKEYBOT_GATEWAY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://{host}:{port}"


async def _run() -> None:
    ensure_gateway_runtime_env()
    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    backend: StorageBackend = create_storage_backend(db_url)
    await backend.open(run_schema=auto_schema_enabled_from_config())
    settings = scheduler_settings()
    worker_id = resolve_scheduler_worker_id()
    base_url = _gateway_base_url()
    invoker = HttpTickInvoker(base_url)
    logger.info(
        "scheduler worker starting worker_id=%s gateway=%s poll_interval_s=%s",
        worker_id,
        base_url,
        settings.poll_interval_s,
    )
    try:
        await run_scheduler_loop(
            store=backend.scheduled_loops(),
            invoker=invoker,
            session_busy=_HttpSessionBusyChecker(invoker),
            ensure_session=_NoopSessionEnsurer(),
            worker_id=worker_id,
            poll_interval_s=settings.poll_interval_s,
            stale_claim_ms=settings.stale_claim_ms,
        )
    finally:
        await backend.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
