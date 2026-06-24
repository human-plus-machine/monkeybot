"""Standalone worker process for queued subagent runs."""

from __future__ import annotations

import asyncio
import logging
import os

from monkeybot.core.logging_utils import normalize_log_level
from monkeybot.core.subagents.worker_pool import run_worker_main


def main() -> None:
    logging.basicConfig(level=normalize_log_level(os.environ.get("LOG_LEVEL"), default="INFO"))
    try:
        asyncio.run(run_worker_main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
