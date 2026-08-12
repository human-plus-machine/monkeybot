"""Helpers for starting the combined (SSE + realtime) gateway from the CLI."""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from pathlib import Path

import httpx

from monkeybot_cli.runtime_python import (
    COMBINED_GATEWAY_MODULE,
    gateway_argv,
    prepare_runtime_python,
)

logger = logging.getLogger(__name__)


def _find_workspace_dir(start: Path | None = None) -> Path | None:
    """Find the directory containing ``monkeybot_config/monkeybot.yaml``."""
    start = start or Path.cwd()
    for path in [start, *start.parents]:
        if (path / "monkeybot_config" / "monkeybot.yaml").exists():
            return path
    return None


def _load_dotenv(workspace_dir: Path) -> None:
    """Load workspace ``.env`` into the current process environment."""
    env_file = workspace_dir / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except Exception:
        logger.warning("Failed to load %s", env_file, exc_info=True)


def _http_base_from_ws_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{host}:{port}"


async def _is_gateway_healthy(url: str, timeout: float = 2.0) -> bool:
    """Return True if ``GET {http_base}/health`` succeeds."""
    base = _http_base_from_ws_url(url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base}/health")
            return resp.status_code == 200
    except Exception as exc:
        logger.debug("Gateway health check failed at %s: %s", base, exc)
        return False


async def _wait_for_gateway(url: str, max_wait: float = 30.0) -> bool:
    """Poll health until ready or timeout."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while asyncio.get_event_loop().time() < deadline:
        if await _is_gateway_healthy(url):
            return True
        await asyncio.sleep(0.5)
    return False


def _url_is_local(url: str) -> bool:
    """Return True if the URL points to localhost/127.0.0.1."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


async def start_gateway_if_needed(
    url: str,
    *,
    start: bool = True,
) -> asyncio.subprocess.Process | None:
    """Start a local combined gateway if requested and not already healthy."""
    if not start:
        return None
    if not _url_is_local(url):
        return None
    if await _is_gateway_healthy(url):
        logger.info("Gateway already reachable at %s", url)
        return None

    workspace = _find_workspace_dir()
    if workspace is None:
        raise RuntimeError(
            "Could not find monkeybot_config/monkeybot.yaml to start the gateway. "
            "Run the command from a MonkeyBot workspace or start the gateway manually."
        )

    _load_dotenv(workspace)
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    env = {**os.environ, "PORT": str(port)}
    env.setdefault("LOG_LEVEL", "error")
    runtime = prepare_runtime_python(workspace)
    cmd = gateway_argv(runtime, module=COMBINED_GATEWAY_MODULE)

    logger.info("Starting combined gateway on port %s from %s", port, workspace)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workspace),
        env=env,
        stdout=None,
        stderr=None,
    )

    if not await _wait_for_gateway(url):
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise RuntimeError(f"Gateway failed to become reachable at {url}")

    logger.info("Gateway ready at %s", url)
    return proc


async def stop_gateway(proc: asyncio.subprocess.Process | None) -> None:
    """Terminate a gateway subprocess started by :func:`start_gateway_if_needed`."""
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
