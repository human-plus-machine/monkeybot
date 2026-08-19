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

Configuration (all via env vars or monkeybot.yaml sandbox.*):
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
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    ExecutionResult,
    SecurityError,
    build_skill_runtime_env,
    validate_mempalace_subcommand,
)

logger = logging.getLogger(__name__)
_ABSOLUTE_PATH_FRAGMENT = re.compile(r"(?<![\w.-])/(?:[^\s'\"`;()]+)")


@dataclass
class SandboxConfig:
    """Configuration for the OpenSandbox integration.

    Populated from environment variables set by monkeybot.yaml or directly.
    Only SANDBOX_ENABLED=true (exact, case-insensitive) activates the sandbox.
    """

    enabled: bool
    server_url: str
    api_key: str | None
    image: str
    ttl_seconds: int
    use_server_proxy: bool = True
    shared_filesystem: bool = True

    @classmethod
    def from_env(cls) -> SandboxConfig:
        raw_ttl = os.getenv("SANDBOX_TTL_SECONDS", "1800")
        try:
            ttl = int(raw_ttl)
        except ValueError:
            raise ValueError(
                f"SANDBOX_TTL_SECONDS must be an integer, got: {raw_ttl!r}"
            ) from None
        api_key = os.getenv("SANDBOX_API_KEY") or os.getenv("SANDBOX_AUTH_TOKEN") or None
        proxy_raw = os.getenv("SANDBOX_USE_SERVER_PROXY", "true").strip().lower()
        use_server_proxy = proxy_raw not in ("0", "false", "no", "off")
        shared_raw = os.getenv("SANDBOX_SHARED_FILESYSTEM", "true").strip().lower()
        shared_filesystem = shared_raw not in ("0", "false", "no", "off")
        return cls(
            enabled=os.getenv("SANDBOX_ENABLED", "false").lower() == "true",
            server_url=os.getenv("SANDBOX_SERVER_URL", "http://localhost:8080"),
            api_key=api_key,
            image=os.getenv(
                "SANDBOX_IMAGE", "python:3.12"
            ),
            ttl_seconds=ttl,
            use_server_proxy=use_server_proxy,
            shared_filesystem=shared_filesystem,
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
        skills_path: Path | None = None,
        artifacts_path: Path | None = None,
        allowed_commands: Sequence[str] | None = None,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root).resolve()
        self._skills_path = Path(skills_path).resolve() if skills_path is not None else None
        self._artifacts_path = (
            Path(artifacts_path).resolve() if artifacts_path is not None else None
        )
        self._sandbox: Any = None
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

    def _remote_requests_mounted_path(self, args: list[str], cwd: Path | str | None) -> bool:
        """Detect host layout paths before dispatching to a compute-only sandbox.

        Shell commands commonly place a pathname inside ``bash -c`` rather
        than pass it as a standalone argument, so inspect shell tokens as well
        as direct absolute arguments. Relative paths are intentionally allowed:
        a remote sandbox resolves them below its own ``/tmp`` workdir.
        """
        mounted_roots = tuple(
            root
            for root in (self._workspace_root, self._skills_path, self._artifacts_path)
            if root is not None
        )
        raw_values = [str(cwd)] if cwd is not None else []
        raw_values.extend(args)
        for raw in raw_values:
            tokens = [raw]
            with suppress(ValueError):
                tokens.extend(shlex.split(raw))
            tokens.extend(_ABSOLUTE_PATH_FRAGMENT.findall(raw))
            if any(
                re.search(rf"(?<![\w.-]){re.escape(str(root))}(?=$|[\s/'\"`;,)])", raw)
                for root in mounted_roots
            ):
                return True
            for token in tokens:
                # Cover shell assignments and long options, e.g. ``PATH=/host/x``.
                candidates = (token, token.split("=", 1)[1]) if "=" in token else (token,)
                for candidate in candidates:
                    path = Path(candidate).expanduser()
                    if not path.is_absolute():
                        continue
                    resolved = path.resolve()
                    if any(resolved == root or root in resolved.parents for root in mounted_roots):
                        return True
        return False

    async def _ensure_sandbox(self) -> None:
        if self._sandbox is not None:
            return

        try:
            from opensandbox import Sandbox
            from opensandbox.config import ConnectionConfig
            from opensandbox.models.sandboxes import Host, Volume
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

        runtime_env = build_skill_runtime_env(cwd=self._workspace_root)
        volumes: list[Any] = []
        mounted_paths: set[str] = set()
        if self._config.shared_filesystem:
            # Derive DNS-safe names from host paths (max 63 chars).
            vol_name = re.sub(r"[^a-z0-9-]", "-", workspace_str.lower()).strip("-")[:63]
            volumes.append(
                Volume(
                    name=vol_name or "workspace",
                    host=Host(path=workspace_str),
                    mountPath=workspace_str,
                    readOnly=False,
                )
            )
            mounted_paths.add(workspace_str)
            if self._skills_path is not None:
                skills_str = str(self._skills_path)
                skills_vol_name = re.sub(r"[^a-z0-9-]", "-", skills_str.lower()).strip("-")[:63]
                volumes.append(
                    Volume(
                        name=skills_vol_name or "skills",
                        host=Host(path=skills_str),
                        mountPath=skills_str,
                        readOnly=True,
                    )
                )
                mounted_paths.add(skills_str)
            if self._artifacts_path is not None:
                artifacts_str = str(self._artifacts_path)
                artifacts_vol_name = (
                    re.sub(r"[^a-z0-9-]", "-", artifacts_str.lower()).strip("-")[:63]
                )
                volumes.append(
                    Volume(
                        name=artifacts_vol_name or "artifacts",
                        host=Host(path=artifacts_str),
                        mountPath=artifacts_str,
                        readOnly=False,
                    )
                )
                mounted_paths.add(artifacts_str)
            for cred_env in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_AUTH_FILE"):
                cred_path = runtime_env.get(cred_env, "").strip()
                if not cred_path or cred_path in mounted_paths:
                    continue
                if not Path(cred_path).is_file():
                    continue
                cred_vol_name = re.sub(r"[^a-z0-9-]", "-", cred_path.lower()).strip("-")[:63]
                volumes.append(
                    Volume(
                        name=cred_vol_name or "gcp-creds",
                        host=Host(path=cred_path),
                        mountPath=cred_path,
                        readOnly=True,
                    )
                )
                mounted_paths.add(cred_path)
        else:
            # A remote OpenSandbox cannot read host paths.  Avoid exporting
            # invalid credentials or layout paths as though they were mounted.
            runtime_env.update(
                {
                    "MONKEYBOT_WORKSPACE_ROOT": "/tmp",
                    "WORKSPACE_ROOT": "/tmp",
                }
            )
            for key in (
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GCP_AUTH_FILE",
                "MONKEYBOT_AGENT_ROOT",
                "SKILLS_PATH",
                "AGENT_MD",
                "MCP_CONFIG",
                "COMMAND_ALLOWLIST_CONFIG",
                "PERMISSION_CONFIG",
            ):
                runtime_env.pop(key, None)

        # MemPalace stays on the host. Do not leak the palace path into the
        # sandbox (it is not mounted, and mempalace search runs on the host).
        for key in ("MEMPALACE_PALACE_PATH", "MEMORY_STORAGE_URI", "MEMORY_PATH"):
            runtime_env.pop(key, None)

        self._sandbox = await Sandbox.create(
            self._config.image,
            connection_config=connection_config,
            timeout=timedelta(seconds=self._config.ttl_seconds),
            env=runtime_env,
            volumes=volumes,
        )
        logger.info("Sandbox created: id=%s", getattr(self._sandbox, "id", "unknown"))

    async def execute(
        self,
        command: str,
        args: list[str],
        *,
        timeout: int = 60,
        cwd: Path | str | None = None,
    ) -> ExecutionResult:
        """Execute a command inside the sandbox container.

        Binary allowlist from ALLOWED_COMMANDS is enforced before the sandbox
        is created or contacted. A blocked command raises SecurityError without
        ever touching the OpenSandbox server.
        """
        if command not in self._allowed_commands:
            raise SecurityError(f"Command '{command}' not allowed")
        if command == "mempalace":
            validate_mempalace_subcommand(args)

        # CoreToolExecutor always passes cwd=workspace root. Compute-only already
        # forces working_directory=/tmp, so the default harness cwd must not trip
        # the mounted-path guard. Nested paths under workspace are still rejected.
        check_cwd = cwd
        if not self._config.shared_filesystem and cwd is not None:
            if Path(cwd).resolve() == self._workspace_root:
                check_cwd = None

        if not self._config.shared_filesystem and self._remote_requests_mounted_path(
            args, check_cwd
        ):
            raise SecurityError(
                "remote sandbox is compute-only and cannot access workspace or skills files"
            )

        await self._ensure_sandbox()

        full_cmd = " ".join([command] + [shlex.quote(a) for a in args])
        logger.info("Sandbox execute: %s", full_cmd)

        from opensandbox.models.execd import RunCommandOpts

        workdir = (
            str(Path(cwd).resolve())
            if cwd is not None and self._config.shared_filesystem
            else "/tmp"
        )
        execution = await self._sandbox.commands.run(
            full_cmd,
            opts=RunCommandOpts(
                timeout=timedelta(seconds=timeout),
                working_directory=workdir,
            ),
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
            await sandbox.kill()
            logger.info("Sandbox destroyed")
        except Exception:
            logger.warning("Failed to destroy sandbox", exc_info=True)
