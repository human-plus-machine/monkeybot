"""Sandbox-backed command executor using OpenSandbox.

Drop-in replacement for TerminalExecutor when SANDBOX_ENABLED=true.
Routes run_command through an isolated Docker container managed by the
OpenSandbox server, while preserving the binary allowlist from TerminalExecutor.

The workspace directory is mounted read-write into the sandbox container at
the same absolute path, so file writes from run_command persist to the host
filesystem immediately and all relative agent paths resolve identically.

The container lives on a :class:`~monkeybot.core.tools.sandbox_session.SandboxSession`.
The SSE gateway passes the session (and the session job table) so one container
serves every turn and background jobs outlive the turn. With neither given,
this executor owns a private session and job table and destroys both on
``aclose`` — voice, Lambda, and subagent workers.

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
import hashlib
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
    JobStatus,
    append_log,
    clamp_job_timeout,
)
from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    ExecutionResult,
    SecurityError,
    _path_candidates,
    _resolved_path,
    validate_mempalace_subcommand,
)

logger = logging.getLogger(__name__)
_ABSOLUTE_PATH_FRAGMENT = re.compile(r"(?<![\w.-])/(?:[^\s'\"`;()]+)")

# SDK contract checked against OpenSandbox ``Commands`` (opensandbox 1.x):
# ``commands.run(..., RunCommandOpts(background=True))`` returns ``Execution.id``.
# execd emits ``execution_complete`` as soon as a background process starts, and
# the SDK stores that on ``Execution.complete`` with ``exit_code`` left unset.
# That is launch acknowledgement, not process exit. ``get_command_status``,
# ``get_background_command_logs(cursor=)``, and ``interrupt`` poll and stop it;
# ``sandbox.renew(timedelta)`` extends lifetime.
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


_VOLUME_NAME_MAX = 63
_VOLUME_HASH_LEN = 8


def _volume_name(path: str, fallback: str) -> str:
    """DNS-label volume name for ``path``: at most 63 chars, unique per path."""
    digest = hashlib.sha1(path.encode(), usedforsecurity=False).hexdigest()[:_VOLUME_HASH_LEN]
    prefix = re.sub(r"[^a-z0-9-]+", "-", path.lower()).strip("-")
    prefix = prefix[: _VOLUME_NAME_MAX - _VOLUME_HASH_LEN - 1].strip("-") or fallback
    return f"{prefix}-{digest}"


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
    prewarm: bool = True

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
        prewarm_raw = env_value_or_current(config, "SANDBOX_PREWARM", "true").strip().lower()
        prewarm = prewarm_raw not in ("0", "false", "no", "off")
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
            prewarm=prewarm,
        )


class SandboxExecutor:
    """Command executor that routes shell commands through an OpenSandbox container.

    Same interface as TerminalExecutor.execute(). Binary allowlist from
    terminal.py is applied before dispatch (defense-in-depth — the container
    provides OS-level isolation, the allowlist enforces the command policy).

    The container is owned by ``session`` when the caller passes one (the SSE
    gateway, one container per chat). Otherwise this executor builds a private
    session and destroys it in ``aclose``. The same split applies to ``jobs``:
    a passed-in table outlives the turn; a private table is killed here.

    Call aclose() on a private executor to destroy the sandbox early; otherwise
    the session reaps it after ttl_seconds with no command and no running job.
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
        session: Any | None = None,
        jobs: Any | None = None,
    ) -> None:
        # Lazy import: sandbox_session imports SandboxConfig from this module.
        from monkeybot.core.tools.sandbox_session import SandboxSession
        from monkeybot.core.tools.session_jobs import SessionJobs

        self._config = config
        self._workspace_root = Path(workspace_root).resolve()
        self._skills_path = Path(skills_path).resolve() if skills_path is not None else None
        self._artifacts_path = (
            Path(artifacts_path).resolve() if artifacts_path is not None else None
        )
        self._owns_session = session is None
        self._session: SandboxSession = session or SandboxSession(
            config,
            self._workspace_root,
            skills_path=self._skills_path,
            artifacts_path=self._artifacts_path,
        )
        self._owns_jobs = jobs is None
        self._jobs: SessionJobs = jobs or SessionJobs(max_jobs=max(1, config.max_background_jobs))
        if self._owns_session:
            self._session.bind_jobs(lambda: bool(self._jobs.running()))
        # Status refresh and kill share this lock. A caller that holds it
        # (tests, and any future critical section) delays the remote interrupt.
        self._lock = asyncio.Lock()
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
    def jobs_survive_turn(self) -> bool:
        """True when the caller owns the job table. ``aclose`` leaves those jobs running."""
        return not self._owns_jobs

    @property
    def _sandbox(self) -> Any:
        return self._session.sandbox

    @property
    def _renew_task(self) -> asyncio.Task[None] | None:
        return self._session.renew_task

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

    def _workdir(self, cwd: Path | str | None) -> str:
        if cwd is not None and self._config.shared_filesystem:
            return str(Path(cwd).resolve())
        return "/tmp"

    async def _open_for_command(self) -> tuple[Any, bool, bool, bool]:
        """Ensure the container and report whether this command reused it."""
        return await self._session.ensure()

    def _log_command(
        self,
        *,
        reused: bool,
        prewarmed: bool,
        first: bool,
        started: float,
    ) -> None:
        extra: dict[str, Any] = {
            "sandbox_reused": reused,
            "sandbox_prewarmed": prewarmed,
            "sandbox_first_command_ms": None,
        }
        if first:
            extra["sandbox_first_command_ms"] = (self._session.now() - started) * 1000.0
        self._session.note_command()
        logger.info("sandbox command", extra=extra)

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

        sandbox, reused, prewarmed, first = await self._open_for_command()

        full_cmd = " ".join([command] + [shlex.quote(a) for a in args])
        logger.info("Sandbox execute: %s", full_cmd)

        from opensandbox.models.execd import RunCommandOpts

        started = self._session.now()
        execution = await sandbox.commands.run(
            full_cmd,
            opts=RunCommandOpts(
                timeout=timedelta(seconds=timeout),
                working_directory=self._workdir(cwd),
            ),
        )
        self._log_command(reused=reused, prewarmed=prewarmed, first=first, started=started)

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
        return self._jobs.has_job(job_id)

    def mark_job_awaited(self, job_id: str) -> None:
        self._jobs.mark_awaited(job_id)

    async def wait_until_settled(self, job_id: str, *, timeout: float) -> None:
        await self._jobs.wait_until_settled(job_id, timeout=timeout)

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
        sandbox: Any,
    ) -> Any:
        logger.info("Sandbox background execute: %s", full_cmd)
        try:
            from opensandbox.models.execd import RunCommandOpts

            return await sandbox.commands.run(
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
        sandbox, reused, prewarmed, first = await self._open_for_command()
        job = self._jobs.allocate(
            command,
            args,
            effective=effective,
            hit_ceiling=hit_ceiling,
            log_dir=log_dir,
        )
        full_cmd = " ".join([command] + [shlex.quote(a) for a in args])
        started = self._session.now()
        try:
            execution = await self._run_remote_background(
                job, full_cmd, self._workdir(cwd), effective, sandbox
            )
        except Exception:
            # The caller sees the start error. Do not also wake the session.
            self._jobs.mark_awaited(job.job_id)
            raise
        self._log_command(reused=reused, prewarmed=prewarmed, first=first, started=started)
        remote_id = getattr(execution, "id", None)
        if not remote_id:
            job.status = JobStatus.LOST
            job.error = "sandbox background command returned no execution id"
            self._jobs.mark_awaited(job.job_id)
            self._log_job(job, "sandbox background job lost", failed=True)
            raise OSError(job.error)
        job.remote_id = str(remote_id)
        # ``complete`` is set at launch. Leave the job running so status and
        # logs are read later; do not invent exit code 0 from a missing one.
        self._capture_execution_logs(job, execution)
        await self._jobs.adopt_remote(job, self._session)
        self._session.poke_renew()
        self._log_job(job, "sandbox background job started")
        return job

    async def get_status(self, job_id: str) -> BackgroundJob:
        async with self._lock:
            return await self._jobs.get_status(job_id)

    async def read_output(self, job_id: str, cursor: int) -> tuple[str, int]:
        return await self._jobs.read_output(job_id, cursor)

    async def kill(self, job_id: str) -> BackgroundJob:
        async with self._lock:
            return await self._jobs.kill(job_id, reason="killed")

    async def aclose(self) -> list[BackgroundJob]:
        """Release this executor.

        A private job table is killed and a private container is destroyed.
        A session-owned table and container are left running. Safe to call
        twice. Returns jobs this call killed.
        """
        killed: list[BackgroundJob] = []
        if self._owns_jobs:
            killed = await self._jobs.aclose("turn end")
        if self._owns_session:
            await self._session.aclose()
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
