"""One OpenSandbox container shared by every turn of a gateway session.

The per-turn :class:`SandboxExecutor` checks the command policy and then asks
this object for the container. Creating the container is single-flight, so a
pre-warm and the first command cannot start two. A failed create is not
remembered. The container is killed when the session closes, when its spec
changes, or after ``ttl_seconds`` with no command and no running job.

A private executor (voice, Lambda, a subagent worker) builds its own session
and closes it at turn end. The SSE gateway keeps one on the session bus.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from monkeybot.core.tools.background_jobs import BackgroundJob, append_log
from monkeybot.core.tools.sandbox_executor import SandboxConfig, _volume_name
from monkeybot.core.tools.terminal import build_skill_runtime_env

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
JobsRunning = Callable[[], bool]


@dataclass(frozen=True)
class SandboxSpec:
    """What a recreate must agree on. TTL and job caps are not part of it."""

    image: str
    server_url: str
    shared_filesystem: bool
    use_server_proxy: bool
    workspace: str
    skills: str | None
    artifacts: str | None

    @classmethod
    def from_config(
        cls,
        config: SandboxConfig,
        workspace_root: Path,
        skills_path: Path | None,
        artifacts_path: Path | None,
    ) -> SandboxSpec:
        return cls(
            image=config.image,
            server_url=config.server_url,
            shared_filesystem=config.shared_filesystem,
            use_server_proxy=config.use_server_proxy,
            workspace=str(Path(workspace_root).resolve()),
            skills=str(Path(skills_path).resolve()) if skills_path is not None else None,
            artifacts=str(Path(artifacts_path).resolve()) if artifacts_path is not None else None,
        )


class SandboxSession:
    """The container, its renew loop, and its idle reaper."""

    def __init__(
        self,
        config: SandboxConfig,
        workspace_root: Path,
        *,
        skills_path: Path | None = None,
        artifacts_path: Path | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        attach_id: str | None = None,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root).resolve()
        self._skills_path = Path(skills_path).resolve() if skills_path is not None else None
        self._artifacts_path = (
            Path(artifacts_path).resolve() if artifacts_path is not None else None
        )
        self._desired = SandboxSpec.from_config(
            config, self._workspace_root, self._skills_path, self._artifacts_path
        )
        self._applied: SandboxSpec | None = None
        self._clock: Clock = clock or time.monotonic
        self._sleep: Sleep = sleep or asyncio.sleep
        self._jobs_running: JobsRunning | None = None
        self._sandbox: Any = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._used = False
        self._commanded = False
        self._created_by_prewarm = False
        self._last_activity = self._clock()
        self._reap_task: asyncio.Task[None] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._prewarm_task: asyncio.Task[None] | None = None
        # Set when this session is a client of a container another process owns.
        # ``aclose`` drops the client and does not kill the container.
        self._attach_id = attach_id or None

    @property
    def spec(self) -> SandboxSpec:
        return self._desired

    @property
    def alive(self) -> bool:
        return self._sandbox is not None

    @property
    def used(self) -> bool:
        """True after a command has run. Pre-warm reads this and never sets it."""
        return self._used

    @property
    def commanded(self) -> bool:
        return self._commanded

    @property
    def created_by_prewarm(self) -> bool:
        return self._created_by_prewarm

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def sandbox(self) -> Any:
        return self._sandbox

    @property
    def attached(self) -> bool:
        """True when this session connected to someone else's container."""
        return self._attach_id is not None

    @property
    def sandbox_id(self) -> str | None:
        """Id of the live container, or the id this session was asked to attach to."""
        if self._sandbox is not None:
            ident = getattr(self._sandbox, "id", None)
            if ident:
                return str(ident)
        return self._attach_id

    @property
    def renew_task(self) -> asyncio.Task[None] | None:
        return self._renew_task

    def bind_jobs(self, running: JobsRunning) -> None:
        """Report whether any job that should keep the container alive is running."""
        self._jobs_running = running

    def replace_spec(self, spec: SandboxSpec) -> None:
        """Ask the next ``ensure`` to recreate when ``spec`` differs."""
        self._desired = spec

    def note_prewarm_task(self, task: asyncio.Task[None]) -> None:
        self._prewarm_task = task

    def command_view(self) -> tuple[bool, bool, bool]:
        """``(reused, prewarmed, first)`` for the container about to run a command."""
        matching = self._sandbox is not None and self._applied == self._desired
        return matching, self._created_by_prewarm and matching, not self._commanded

    def now(self) -> float:
        return self._clock()

    def note_command(self) -> None:
        self._commanded = True
        self._used = True
        self._last_activity = self._clock()

    async def ensure(self, *, prewarm: bool = False) -> tuple[Any, bool, bool, bool]:
        """Return ``(container, reused, prewarmed, first_command)``.

        The three flags are decided under the same lock as creation, so two
        commands cannot both observe a cold container.
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("sandbox session is closed")
            if self._sandbox is not None and self._applied != self._desired:
                await self._kill_unlocked()
            reused = self._sandbox is not None and self._applied == self._desired
            prewarmed = self._created_by_prewarm and reused
            first = not self._commanded
            if self._sandbox is None:
                await self._create_unlocked(prewarm=prewarm)
                reused = False
                prewarmed = False
                first = True
            return self._sandbox, reused, prewarmed, first

    async def release(self) -> None:
        """Destroy the container and keep the session. ``used`` stays set."""
        self._cancel_reap()
        async with self._lock:
            await self._kill_unlocked()

    async def prewarm(self) -> None:
        """Start the container ahead of the model call. Failures are logged."""
        try:
            await self.ensure(prewarm=True)
        except Exception:
            logger.warning("sandbox prewarm failed", exc_info=True)

    def poke_renew(self) -> None:
        """Extend the container while background jobs are running."""
        if self._renew_task is not None and not self._renew_task.done():
            return
        self._renew_task = asyncio.create_task(self._renew_while_jobs(), name="sandbox-renew")

    async def status(self, remote_id: str) -> Any:
        sandbox = self._sandbox
        if sandbox is None:
            raise RuntimeError("sandbox is gone")
        return await sandbox.commands.get_command_status(remote_id)

    async def pull_logs(self, job: BackgroundJob) -> None:
        sandbox = self._sandbox
        if sandbox is None or not job.remote_id:
            return
        try:
            logs = await sandbox.commands.get_background_command_logs(
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

    async def interrupt(self, remote_id: str) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            raise RuntimeError("sandbox is gone")
        await sandbox.commands.interrupt(remote_id)

    async def aclose(self) -> None:
        """Kill the container once. A second call does nothing.

        An attached session drops its client and leaves the container running.
        The parent session is the one that kills it.
        """
        if self._closed and self._sandbox is None:
            return
        self._closed = True
        prewarm = self._prewarm_task
        self._prewarm_task = None
        if prewarm is not None and not prewarm.done():
            prewarm.cancel()
        self._cancel_renew()
        self._cancel_reap()
        async with self._lock:
            if self._attach_id:
                self._sandbox = None
                self._applied = None
                return
            await self._kill_unlocked()

    async def _create_unlocked(self, *, prewarm: bool) -> None:
        started = self._clock()
        try:
            from opensandbox import Sandbox
            from opensandbox.config import ConnectionConfig
            from opensandbox.models.sandboxes import Host, Volume
        except ImportError as exc:
            raise RuntimeError(
                "opensandbox SDK is required for sandbox execution but is not installed. "
                "Install it with: pip install 'monkeybot[sandbox]'"
            ) from exc

        spec = self._desired
        logger.info(
            "Creating sandbox: image=%s ttl=%ss workspace=%s",
            spec.image,
            self._config.ttl_seconds,
            spec.workspace,
        )
        match = re.fullmatch(r"(https?)://(.+)", spec.server_url.rstrip("/"))
        if not match:
            raise ValueError(
                f"SANDBOX_SERVER_URL must be http(s)://host[:port], got: {spec.server_url!r}"
            )
        protocol, domain = match.group(1), match.group(2)
        connection_config = ConnectionConfig(
            domain=domain,
            api_key=self._config.api_key,
            protocol=protocol,
            use_server_proxy=spec.use_server_proxy,
        )
        runtime_env, volumes = self._runtime_and_volumes(Host, Volume)
        try:
            if self._attach_id:
                sandbox = await Sandbox.connect(
                    self._attach_id,
                    connection_config=connection_config,
                    skip_health_check=True,
                )
            else:
                sandbox = await Sandbox.create(
                    spec.image,
                    connection_config=connection_config,
                    timeout=timedelta(seconds=self._config.ttl_seconds),
                    env=runtime_env,
                    volumes=volumes,
                )
        except Exception:
            # A failed create must not stick. The next ensure tries again.
            self._sandbox = None
            raise
        self._sandbox = sandbox
        self._applied = spec
        self._created_by_prewarm = prewarm
        self._commanded = False
        self._last_activity = self._clock()
        elapsed_ms = (self._clock() - started) * 1000.0
        logger.info(
            "sandbox created",
            extra={
                "sandbox_create_ms": elapsed_ms,
                "sandbox_id": getattr(sandbox, "id", "unknown"),
                "sandbox_prewarmed": prewarm,
            },
        )
        if not self._attach_id:
            self._arm_reap()

    def _runtime_and_volumes(
        self, host_cls: Any, volume_cls: Any
    ) -> tuple[dict[str, str], list[Any]]:
        spec = self._desired
        runtime_env = build_skill_runtime_env(cwd=spec.workspace)
        volumes: list[Any] = []
        mounted_paths: set[str] = set()
        if spec.shared_filesystem:
            volumes.append(
                volume_cls(
                    name=_volume_name(spec.workspace, "workspace"),
                    host=host_cls(path=spec.workspace),
                    mountPath=spec.workspace,
                    readOnly=False,
                )
            )
            mounted_paths.add(spec.workspace)
            if spec.skills is not None:
                volumes.append(
                    volume_cls(
                        name=_volume_name(spec.skills, "skills"),
                        host=host_cls(path=spec.skills),
                        mountPath=spec.skills,
                        readOnly=True,
                    )
                )
                mounted_paths.add(spec.skills)
            if spec.artifacts is not None:
                volumes.append(
                    volume_cls(
                        name=_volume_name(spec.artifacts, "artifacts"),
                        host=host_cls(path=spec.artifacts),
                        mountPath=spec.artifacts,
                        readOnly=False,
                    )
                )
                mounted_paths.add(spec.artifacts)
            for cred_env in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_AUTH_FILE"):
                cred_path = runtime_env.get(cred_env, "").strip()
                if not cred_path or cred_path in mounted_paths:
                    continue
                if not Path(cred_path).is_file():
                    continue
                volumes.append(
                    volume_cls(
                        name=_volume_name(cred_path, "gcp-creds"),
                        host=host_cls(path=cred_path),
                        mountPath=cred_path,
                        readOnly=True,
                    )
                )
                mounted_paths.add(cred_path)
        else:
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
        for key in ("MEMPALACE_PALACE_PATH", "MEMORY_STORAGE_URI", "MEMORY_PATH"):
            runtime_env.pop(key, None)
        return runtime_env, volumes

    def _arm_reap(self) -> None:
        self._cancel_reap()
        self._reap_task = asyncio.create_task(self._reap_when_idle(), name="sandbox-reap")

    def _cancel_reap(self) -> None:
        task = self._reap_task
        self._reap_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _reap_when_idle(self) -> None:
        """Kill the container after a quiet TTL. A running job keeps it."""
        try:
            while not self._closed and self._sandbox is not None:
                await self._sleep(self._config.ttl_seconds)
                async with self._lock:
                    if self._closed or self._sandbox is None:
                        return
                    if self._jobs_running is not None and self._jobs_running():
                        continue
                    idle = self._clock() - self._last_activity
                    if idle < self._config.ttl_seconds:
                        continue
                    logger.info(
                        "sandbox idle reap",
                        extra={"idle_seconds": idle, "ttl_seconds": self._config.ttl_seconds},
                    )
                    await self._kill_unlocked()
                    return
        except asyncio.CancelledError:
            return

    def _cancel_renew(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _renew_while_jobs(self) -> None:
        """Extend the worker while any bound job is still running.

        Renews once immediately, then every ``renew_interval_seconds``. An
        interval of 0 renews once and returns. Failures are logged.
        """
        try:
            await self._renew_once()
            interval = self._config.renew_interval_seconds
            while (
                interval > 0
                and self._sandbox is not None
                and self._jobs_running is not None
                and self._jobs_running()
            ):
                await self._sleep(interval)
                if self._sandbox is None or self._jobs_running is None or not self._jobs_running():
                    return
                await self._renew_once()
        except asyncio.CancelledError:
            return

    async def _renew_once(self) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            return
        try:
            await sandbox.renew(timedelta(seconds=self._config.ttl_seconds))
            logger.info(
                "sandbox renewed while background jobs run",
                extra={"ttl_seconds": self._config.ttl_seconds},
            )
        except Exception:
            logger.warning("sandbox renew failed while background jobs run", exc_info=True)

    async def _kill_unlocked(self) -> None:
        sandbox = self._sandbox
        self._sandbox = None
        self._applied = None
        self._commanded = False
        self._created_by_prewarm = False
        if sandbox is None:
            return
        try:
            await sandbox.kill()
            logger.info("Sandbox destroyed")
        except Exception:
            logger.warning("Failed to destroy sandbox", exc_info=True)
