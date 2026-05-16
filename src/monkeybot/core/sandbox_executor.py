"""Sandbox-backed command executor using OpenSandbox.

Drop-in replacement for TerminalExecutor when SANDBOX_ENABLED=true.
Routes run_command through an isolated Docker container managed by the
OpenSandbox server, while preserving the binary allowlist from TerminalExecutor.

The workspace directory is mounted read-write into the sandbox container at
the same absolute path, so file writes from run_command persist to the host
filesystem immediately and all relative agent paths resolve identically.

One sandbox is created per SandboxExecutor instance (session-scoped) and
reused across all run_command calls in that session. The sandbox is created
lazily on the first execute() call so there is zero overhead when run_command
is never invoked.

Configuration (all via env vars or bot.yaml sandbox.*):
    SANDBOX_ENABLED            - "true" to activate (default: "false")
    SANDBOX_SERVER_URL         - OpenSandbox server URL (default: http://localhost:8080)
    SANDBOX_IMAGE              - Container image to run (default: python:3.12)
    SANDBOX_API_KEY            - API key for the server (env-only, optional)
    SANDBOX_TTL_SECONDS        - Sandbox lifetime in seconds (env-only, default: 1800)
    SANDBOX_USE_SERVER_PROXY   - default "true": route health/exec via OpenSandbox HTTP API
      (required for Docker Desktop when the server runs in a container). "false" if the SDK
      host can reach sandbox ports directly.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from monkeybot.core.terminal import ALLOWED_PATHS, ALLOWED_COMMANDS, ExecutionResult, SecurityError

logger = logging.getLogger(__name__)


@dataclass
class SandboxConfig:
    """Configuration for the OpenSandbox integration.

    Populated from environment variables set by bot.yaml or directly.
    Only SANDBOX_ENABLED=true (exact, case-insensitive) activates the sandbox.
    """

    enabled: bool
    server_url: str
    api_key: str | None
    image: str
    ttl_seconds: int
    use_server_proxy: bool = True

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        raw_ttl = os.getenv("SANDBOX_TTL_SECONDS", "1800")
        try:
            ttl = int(raw_ttl)
        except ValueError:
            raise ValueError(
                f"SANDBOX_TTL_SECONDS must be an integer, got: {raw_ttl!r}"
            )
        api_key = os.getenv("SANDBOX_API_KEY") or None
        proxy_raw = os.getenv("SANDBOX_USE_SERVER_PROXY", "true").strip().lower()
        use_server_proxy = proxy_raw not in ("0", "false", "no", "off")
        return cls(
            enabled=os.getenv("SANDBOX_ENABLED", "false").lower() == "true",
            server_url=os.getenv("SANDBOX_SERVER_URL", "http://localhost:8080"),
            api_key=api_key,
            image=os.getenv("SANDBOX_IMAGE", "python:3.12"),
            ttl_seconds=ttl,
            use_server_proxy=use_server_proxy,
        )


class SandboxExecutor:
    """Command executor that routes shell commands through an OpenSandbox container.

    Same interface as TerminalExecutor.execute(). Binary allowlist from
    terminal.py is applied before dispatch (defense-in-depth — the container
    provides OS-level isolation, the allowlist enforces the command policy).

    The sandbox is created lazily on the first execute() call and reused for
    the lifetime of this instance (session-scoped). The workspace directory is
    mounted read-write at its absolute path inside the container so file writes
    persist to the host filesystem immediately.

    Call aclose() to destroy the sandbox early; otherwise it expires after
    ttl_seconds automatically.
    """

    def __init__(
        self,
        config: SandboxConfig,
        workspace_root: Path,
        *,
        allowed_commands: Sequence[str] | None = None,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root).resolve()
        self._sandbox: object | None = None
        self._allowed_commands: tuple[str, ...] = (
            tuple(allowed_commands) if allowed_commands is not None else tuple(ALLOWED_COMMANDS)
        )

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        return self._allowed_commands

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        """Path prefixes allowed for ``run_command`` error hints (host uses :class:`TerminalExecutor`)."""
        return tuple(ALLOWED_PATHS)

    async def _ensure_sandbox(self) -> None:
        if self._sandbox is not None:
            return

        try:
            from opensandbox import Sandbox  # type: ignore[import]
            from opensandbox.config import ConnectionConfig  # type: ignore[import]
            from opensandbox.models.sandboxes import Host, Volume  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "opensandbox SDK is required for sandbox execution but is not installed. "
                "Install it with: pip install 'monkeybot[sandbox]'"
            ) from exc

        workspace_str = str(self._workspace_root)
        logger.info(
            "Creating sandbox: image=%s ttl=%ss workspace=%s",
            self._config.image,
            self._config.ttl_seconds,
            workspace_str,
        )

        # Parse server_url into domain + protocol for ConnectionConfig.
        # server_url format: "http://host:port" or "https://host:port"
        match = re.fullmatch(r"(https?)://(.+)", self._config.server_url.rstrip("/"))
        if not match:
            raise ValueError(
                f"SANDBOX_SERVER_URL must be http(s)://host[:port], got: {self._config.server_url!r}"
            )
        protocol, domain = match.group(1), match.group(2)

        connection_config = ConnectionConfig(
            domain=domain,
            api_key=self._config.api_key,
            protocol=protocol,
            use_server_proxy=self._config.use_server_proxy,
        )

        # Derive a DNS-safe volume name from the workspace path (max 63 chars).
        vol_name = re.sub(r"[^a-z0-9-]", "-", workspace_str.lower()).strip("-")[:63]

        self._sandbox = await Sandbox.create(
            self._config.image,
            connection_config=connection_config,
            timeout=timedelta(seconds=self._config.ttl_seconds),
            volumes=[
                Volume(
                    name=vol_name or "workspace",
                    host=Host(path=workspace_str),
                    mountPath=workspace_str,
                    readOnly=False,
                )
            ],
        )
        logger.info("Sandbox created: id=%s", getattr(self._sandbox, "id", "unknown"))

    async def execute(
        self,
        command: str,
        args: list[str],
        *,
        timeout: int = 60,
    ) -> ExecutionResult:
        """Execute a command inside the sandbox container.

        Binary allowlist from ALLOWED_COMMANDS is enforced before the sandbox
        is created or contacted. A blocked command raises SecurityError without
        ever touching the OpenSandbox server.
        """
        if command not in self._allowed_commands:
            raise SecurityError(f"Command '{command}' not allowed")

        await self._ensure_sandbox()

        full_cmd = " ".join([command] + [shlex.quote(a) for a in args])
        logger.info("Sandbox execute: %s", full_cmd)

        from opensandbox.models.execd import RunCommandOpts  # type: ignore[import]

        execution = await self._sandbox.commands.run(  # type: ignore[union-attr]
            full_cmd,
            opts=RunCommandOpts(timeout=timedelta(seconds=timeout)),
        )

        stdout_entries = execution.logs.stdout or []
        stderr_entries = execution.logs.stderr or []
        stdout = "".join(getattr(e, "text", str(e)) for e in stdout_entries)
        stderr = "".join(getattr(e, "text", str(e)) for e in stderr_entries)

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=execution.exit_code if execution.exit_code is not None else 0,
        )

    async def aclose(self) -> None:
        """Destroy the sandbox container.

        Safe to call multiple times (idempotent). Exceptions from kill() are
        swallowed so the gateway finally block always completes cleanly.
        """
        if self._sandbox is None:
            return
        sandbox = self._sandbox
        self._sandbox = None
        try:
            await sandbox.kill()  # type: ignore[union-attr]
            logger.info("Sandbox destroyed")
        except Exception:
            logger.warning("Failed to destroy sandbox", exc_info=True)
