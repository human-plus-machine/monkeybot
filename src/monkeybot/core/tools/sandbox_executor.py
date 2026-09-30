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

import asyncio
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

from monkeybot.core.config.snapshot import RuntimeConfig, env_value_or_current
from monkeybot.core.tools.background_jobs import (
    DEFAULT_AWAIT_MAX_WAIT_SECONDS,
    DEFAULT_AWAIT_WAIT_SECONDS,
    DEFAULT_MAX_BACKGROUND_JOBS,
    DEFAULT_MAX_JOB_SECONDS,
    DEFAULT_RENEW_INTERVAL_SECONDS,
    BackgroundJob,
    JobRegistry,
    JobStatus,
    allocate_background_job,
    append_log,
    clamp_job_timeout,
    read_log_from,
    timeout_failure_message,
)
from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    ExecutionResult,
    SecurityError,
    _path_candidates,
    _resolved_path,
    build_skill_runtime_env,
    validate_mempalace_subcommand,
)

logger = logging.getLogger(__name__)
_ABSOLUTE_PATH_FRAGMENT = re.compile(r"(?<![\w.-])/(?:[^\s'\"`;()]+)")

# SDK contract checked against OpenSandbox ``Commands`` (opensandbox 1.x):
# ``commands.run(..., RunCommandOpts(background=True))`` returns ``Execution.id``;
# ``get_command_status``, ``get_background_command_logs(cursor=)``, and
# ``interrupt`` poll and stop it; ``sandbox.renew(timedelta)`` extends lifetime.
# Live execd v1.0.22 on dev-internal is not reachable from this repo. If those
# endpoints are missing, status refresh marks the job ``lost`` and returns the
# server error instead of holding the turn open.
# ``max_sandbox_timeout_seconds`` may still cap ``renew()``; that is a server
# setting, not something this process can prove. Renew failures are logged and
# the job continues until the worker is reaped or the local ceiling fires.


def _int_at_least(raw: str, default: int, min_: int) -> int:
    """Parse ``raw`` as an int, falling back to ``default`` when invalid or below ``min_``."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= min_ else default


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
    max_background_jobs: int = DEFAULT_MAX_BACKGROUND_JOBS
    max_job_seconds: int = DEFAULT_MAX_JOB_SECONDS
    renew_interval_seconds: int = DEFAULT_RENEW_INTERVAL_SECONDS
    await_default_wait_seconds: int = DEFAULT_AWAIT_WAIT_SECONDS
    await_max_wait_seconds: int = DEFAULT_AWAIT_MAX_WAIT_SECONDS

    @classmethod
    def from_env(cls, config: RuntimeConfig | None = None) -> SandboxConfig:
        """Build from a pinned snapshot, else the process-wide current snapshot."""
        raw_ttl = env_value_or_current(config, "SANDBOX_TTL_SECONDS", "1800")
        try:
            ttl = int(raw_ttl)
        except ValueError:
            raise ValueError(f"SANDBOX_TTL_SECONDS must be an integer, got: {raw_ttl!r}") from None
        api_key = os.getenv("SANDBOX_API_KEY") or os.getenv("SANDBOX_AUTH_TOKEN") or None
        proxy_raw = os.getenv("SANDBOX_USE_SERVER_PROXY", "true").strip().lower()
        use_server_proxy = proxy_raw not in ("0", "false", "no", "off")
        shared_raw = (
            env_value_or_current(config, "SANDBOX_SHARED_FILESYSTEM", "true").strip().lower()
        )
        shared_filesystem = shared_raw not in ("0", "false", "no", "off")
        max_jobs = _int_at_least(
            env_value_or_current(
                config, "SANDBOX_MAX_BACKGROUND_JOBS", str(DEFAULT_MAX_BACKGROUND_JOBS)
            ),
            DEFAULT_MAX_BACKGROUND_JOBS,
            1,
        )
        max_job_seconds = _int_at_least(
            env_value_or_current(config, "SANDBOX_MAX_JOB_SECONDS", str(DEFAULT_MAX_JOB_SECONDS)),
            DEFAULT_MAX_JOB_SECONDS,
            1,
        )
        renew_interval = _int_at_least(
            env_value_or_current(
                config, "SANDBOX_RENEW_INTERVAL_SECONDS", str(DEFAULT_RENEW_INTERVAL_SECONDS)
            ),
            DEFAULT_RENEW_INTERVAL_SECONDS,
            0,
        )
        await_default = _int_at_least(
            env_value_or_current(
                config, "SANDBOX_AWAIT_DEFAULT_WAIT_SECONDS", str(DEFAULT_AWAIT_WAIT_SECONDS)
            ),
            DEFAULT_AWAIT_WAIT_SECONDS,
            1,
        )
        await_max = _int_at_least(
            env_value_or_current(
                config, "SANDBOX_AWAIT_MAX_WAIT_SECONDS", str(DEFAULT_AWAIT_MAX_WAIT_SECONDS)
            ),
            DEFAULT_AWAIT_MAX_WAIT_SECONDS,
            1,
        )
        if await_default > await_max:
            await_default = await_max
        return cls(
            enabled=env_value_or_current(config, "SANDBOX_ENABLED", "false").lower() == "true",
            server_url=env_value_or_current(config, "SANDBOX_SERVER_URL", "http://localhost:8080"),
            api_key=api_key,
            image=env_value_or_current(config, "SANDBOX_IMAGE", "python:3.12"),
            ttl_seconds=ttl,
            use_server_proxy=use_server_proxy,
            shared_filesystem=shared_filesystem,
            max_background_jobs=max_jobs,
            max_job_seconds=max_job_seconds,
            renew_interval_seconds=renew_interval,
            await_default_wait_seconds=await_default,
            await_max_wait_seconds=await_max,
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
        allowed_path_prefixes: Sequence[str] | None = None,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root).resolve()
        self._skills_path = Path(skills_path).resolve() if skills_path is not None else None
        self._artifacts_path = (
            Path(artifacts_path).resolve() if artifacts_path is not None else None
        )
        self._sandbox: Any = None
        self._jobs = JobRegistry(max_jobs=max(1, config.max_background_jobs))
        self._lock = asyncio.Lock()
        self._renew_task: asyncio.Task[None] | None = None
        self._timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self._allowed_commands: tuple[str, ...] = (
            tuple(allowed_commands) if allowed_commands is not None else tuple(ALLOWED_COMMANDS)
        )
        self._allowed_path_prefixes_value: tuple[str, ...] = (
            tuple(allowed_path_prefixes)
            if allowed_path_prefixes is not None
            else tuple(ALLOWED_PATHS)
        )

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        return self._allowed_commands

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        """Path prefixes allowed for ``run_command`` — same argv pre-flight
        screen as ``TerminalExecutor._validate_paths`` (reusing the same
        module-level helpers), applied here too so the two executors agree rather than
        the container's own mount layout being the only thing enforcing
        this. With the default ``shared_filesystem: true`` the container is
        incidentally confined to the bind-mounted workspace regardless; this
        is defense-in-depth, not the primary boundary (see BACKLOG "Sandbox
        Workspace Protection")."""
        return self._allowed_path_prefixes_value

    def _validate_paths(self, args: list[str], *, command: str, cwd: Path | str | None) -> None:
        base = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
        allowed_roots = tuple(
            _resolved_path(prefix, cwd=base) for prefix in self._allowed_path_prefixes_value
        )
        for arg in _path_candidates(command, args):
            candidate = _resolved_path(arg, cwd=base)
            if any(candidate == root or root in candidate.parents for root in allowed_roots):
                continue
            raise SecurityError(f"Path '{arg}' not allowed")

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
                artifacts_vol_name = re.sub(r"[^a-z0-9-]", "-", artifacts_str.lower()).strip("-")[
                    :63
                ]
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
        extra_allowed_commands: Sequence[str] | None = None,
    ) -> ExecutionResult:
        """Execute a command inside the sandbox container.

        Binary allowlist from ALLOWED_COMMANDS (plus any per-call
        ``extra_allowed_commands`` grant — see ``TerminalExecutor._validate_command``)
        is enforced before the sandbox is created or contacted. A blocked
        command raises SecurityError without ever touching the OpenSandbox
        server.
        """
        self._screen_command(command, args, cwd=cwd, extra_allowed_commands=extra_allowed_commands)

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

    def running_jobs(self) -> list[BackgroundJob]:
        return self._jobs.running()

    def has_job(self, job_id: str) -> bool:
        return job_id in self._jobs

    def _log_job(
        self,
        job: BackgroundJob,
        message: str,
        *,
        failed: bool = False,
        exc_info: bool = False,
    ) -> None:
        log = logger.warning if failed else logger.info
        log(
            message,
            extra={
                "job_id": job.job_id,
                "remote_id": job.remote_id,
                "status": job.status.value,
                "exit_code": job.exit_code,
            },
            exc_info=exc_info,
        )

    async def _run_remote_background(
        self,
        job: BackgroundJob,
        full_cmd: str,
        workdir: str,
        effective: int,
    ) -> Any:
        logger.info("Sandbox background execute: %s", full_cmd)
        try:
            from opensandbox.models.execd import RunCommandOpts

            return await self._sandbox.commands.run(
                full_cmd,
                opts=RunCommandOpts(
                    background=True,
                    timeout=timedelta(seconds=effective),
                    working_directory=workdir,
                ),
            )
        except Exception:
            job.status = JobStatus.LOST
            job.error = "failed to start background command"
            self._log_job(
                job,
                "sandbox background command failed to start",
                failed=True,
                exc_info=True,
            )
            raise

    async def start_background(
        self,
        command: str,
        args: list[str],
        *,
        timeout: int = 60,
        cwd: Path | str | None = None,
        extra_allowed_commands: Sequence[str] | None = None,
        log_dir: Path | None = None,
    ) -> BackgroundJob:
        """Start a detached sandbox command. Same allowlist as ``execute``."""
        effective, hit_ceiling = clamp_job_timeout(timeout, ceiling=self._config.max_job_seconds)
        self._screen_command(command, args, cwd=cwd, extra_allowed_commands=extra_allowed_commands)
        await self._ensure_sandbox()
        job = allocate_background_job(
            self._jobs,
            command,
            args,
            effective=effective,
            hit_ceiling=hit_ceiling,
            log_dir=log_dir,
        )
        full_cmd = " ".join([command] + [shlex.quote(a) for a in args])
        workdir = (
            str(Path(cwd).resolve())
            if cwd is not None and self._config.shared_filesystem
            else "/tmp"
        )
        execution = await self._run_remote_background(job, full_cmd, workdir, effective)
        remote_id = getattr(execution, "id", None)
        if not remote_id:
            job.status = JobStatus.LOST
            job.error = "sandbox background command returned no execution id"
            self._log_job(job, "sandbox background job lost", failed=True)
            raise OSError(job.error)
        job.remote_id = str(remote_id)
        self._capture_execution_logs(job, execution)
        finished = (
            execution.exit_code is not None or getattr(execution, "complete", None) is not None
        )
        if finished:
            job.status = JobStatus.EXITED
            job.exit_code = execution.exit_code if execution.exit_code is not None else 0
            self._log_job(job, "sandbox background job exited")
            return job
        self._ensure_renew_loop()
        self._timeout_tasks[job.job_id] = asyncio.create_task(
            self._enforce_timeout(job.job_id),
            name=f"sandbox-job-timeout-{job.job_id}",
        )
        self._log_job(job, "sandbox background job started")
        return job

    async def get_status(self, job_id: str) -> BackgroundJob:
        async with self._lock:
            job = self._jobs.get(job_id)
            await self._refresh_unlocked(job)
            return job

    async def read_output(self, job_id: str, cursor: int) -> tuple[str, int]:
        job = self._jobs.get(job_id)
        return read_log_from(job.log_path, cursor, final=job.status is not JobStatus.RUNNING)

    async def kill(self, job_id: str) -> BackgroundJob:
        job = self._jobs.get(job_id)
        async with self._lock:
            if job.status is JobStatus.RUNNING:
                await self._stop_remote(job, reason="killed")
        return job

    async def aclose(self) -> list[BackgroundJob]:
        """Interrupt remaining jobs, then destroy the sandbox.

        Safe to call multiple times. Exceptions from kill() are swallowed so
        the gateway finally block always completes. Returns jobs that were
        still running (now killed or lost).
        """
        self._cancel_renew()
        killed: list[BackgroundJob] = []
        for job in list(self._jobs.running()):
            try:
                killed.append(await self.kill(job.job_id))
            except Exception:
                logger.warning(
                    "failed to interrupt background job during aclose",
                    extra={"job_id": job.job_id},
                    exc_info=True,
                )
                if job.status is JobStatus.RUNNING:
                    job.status = JobStatus.LOST
                    job.error = job.error or "lost during shutdown"
                killed.append(job)
        for task in list(self._timeout_tasks.values()):
            task.cancel()
        self._timeout_tasks.clear()
        if self._sandbox is None:
            return killed
        sandbox = self._sandbox
        self._sandbox = None
        try:
            await sandbox.kill()
            logger.info("Sandbox destroyed")
        except Exception:
            logger.warning("Failed to destroy sandbox", exc_info=True)
        return killed

    def _screen_command(
        self,
        command: str,
        args: list[str],
        *,
        cwd: Path | str | None,
        extra_allowed_commands: Sequence[str] | None,
    ) -> None:
        """Allowlist and path checks. Raises before the sandbox is contacted."""
        if command not in self._allowed_commands and command not in (extra_allowed_commands or ()):
            raise SecurityError(f"Command '{command}' not allowed")
        if command == "mempalace":
            validate_mempalace_subcommand(args)
        # CoreToolExecutor always passes cwd=workspace root. Compute-only already
        # forces working_directory=/tmp, so the default harness cwd must not trip
        # the mounted-path guard. Nested paths under workspace are still rejected.
        check_cwd = cwd
        if (
            not self._config.shared_filesystem
            and cwd is not None
            and Path(cwd).resolve() == self._workspace_root
        ):
            check_cwd = None
        if not self._config.shared_filesystem and self._remote_requests_mounted_path(
            args, check_cwd
        ):
            raise SecurityError(
                "remote sandbox is compute-only and cannot access workspace or skills files"
            )
        self._validate_paths(args, command=command, cwd=cwd)

    def _capture_execution_logs(self, job: BackgroundJob, execution: Any) -> None:
        logs = getattr(execution, "logs", None)
        if logs is None:
            return
        stdout = "".join(getattr(entry, "text", str(entry)) for entry in (logs.stdout or []))
        stderr = "".join(getattr(entry, "text", str(entry)) for entry in (logs.stderr or []))
        if stdout:
            append_log(job.log_path, stdout if stdout.endswith("\n") else stdout + "\n")
        if stderr:
            append_log(job.log_path, stderr if stderr.endswith("\n") else stderr + "\n")

    async def _refresh_unlocked(self, job: BackgroundJob) -> None:
        if job.status is not JobStatus.RUNNING or not job.remote_id or self._sandbox is None:
            return
        try:
            status = await self._sandbox.commands.get_command_status(job.remote_id)
        except Exception as exc:
            self._log_job(job, "sandbox command status failed", failed=True, exc_info=True)
            # Status is unknown, so stop the remote process rather than leave it
            # running unseen; ``_stop_remote`` marks the job lost if that fails too.
            await self._stop_remote(job, reason=str(exc) or "sandbox command status failed")
            return
        await self._pull_remote_logs(job)
        running = getattr(status, "running", None)
        exit_code = getattr(status, "exit_code", None)
        if running:
            return
        self._cancel_timeout(job.job_id)
        if exit_code is None and not getattr(status, "error", None):
            job.status = JobStatus.LOST
            job.error = "sandbox command ended without an exit code"
            self._log_job(job, "sandbox background job lost", failed=True)
            return
        job.exit_code = exit_code if exit_code is not None else 0
        job.status = JobStatus.EXITED
        error = getattr(status, "error", None)
        if error and job.exit_code != 0:
            job.error = str(error)
        self._log_job(job, "sandbox background job exited")

    async def _pull_remote_logs(self, job: BackgroundJob) -> None:
        if self._sandbox is None or not job.remote_id:
            return
        try:
            logs = await self._sandbox.commands.get_background_command_logs(
                job.remote_id, cursor=job.remote_cursor
            )
        except Exception:
            logger.warning(
                "sandbox background log read failed",
                extra={"job_id": job.job_id, "remote_id": job.remote_id},
                exc_info=True,
            )
            return
        content = getattr(logs, "content", "") or ""
        if content:
            append_log(job.log_path, content if content.endswith("\n") else content + "\n")
        cursor = getattr(logs, "cursor", None)
        if cursor is not None:
            job.remote_cursor = int(cursor)

    async def _stop_remote(self, job: BackgroundJob, *, reason: str) -> None:
        self._cancel_timeout(job.job_id)
        if job.status is not JobStatus.RUNNING:
            return
        if self._sandbox is not None and job.remote_id:
            try:
                await self._sandbox.commands.interrupt(job.remote_id)
            except Exception:
                job.status = JobStatus.LOST
                job.error = reason
                self._log_job(job, "sandbox interrupt failed", failed=True, exc_info=True)
                return
        job.status = JobStatus.KILLED
        job.error = reason
        if job.exit_code is None:
            job.exit_code = -1
        append_log(job.log_path, f"\n[{reason}]\n")
        self._log_job(job, "sandbox background job killed")

    async def _enforce_timeout(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        try:
            await asyncio.sleep(job.timeout_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if job.status is JobStatus.RUNNING:
                await self._stop_remote(
                    job,
                    reason=timeout_failure_message(
                        timeout_seconds=job.timeout_seconds,
                        hit_ceiling=job.hit_ceiling,
                    ),
                )

    def _cancel_timeout(self, job_id: str) -> None:
        task = self._timeout_tasks.pop(job_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _ensure_renew_loop(self) -> None:
        if self._renew_task is not None and not self._renew_task.done():
            return
        self._renew_task = asyncio.create_task(self._renew_while_jobs(), name="sandbox-renew")

    def _cancel_renew(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _renew_while_jobs(self) -> None:
        """Extend the worker while any background job is still running.

        Renews once immediately, then every ``renew_interval_seconds``. An
        interval of 0 renews once and returns (useful when the server rejects
        further renews). Failures are logged; the local job ceiling still applies.
        """
        try:
            await self._renew_once()
            interval = self._config.renew_interval_seconds
            while interval > 0 and self.running_jobs() and self._sandbox is not None:
                await asyncio.sleep(interval)
                if not self.running_jobs() or self._sandbox is None:
                    return
                await self._renew_once()
        except asyncio.CancelledError:
            return

    async def _renew_once(self) -> None:
        if self._sandbox is None:
            return
        try:
            await self._sandbox.renew(timedelta(seconds=self._config.ttl_seconds))
            logger.info(
                "sandbox renewed while background jobs run",
                extra={"ttl_seconds": self._config.ttl_seconds},
            )
        except Exception:
            logger.warning("sandbox renew failed while background jobs run", exc_info=True)
