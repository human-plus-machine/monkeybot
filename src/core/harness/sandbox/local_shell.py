"""LocalShellSandbox — best-effort host-local sandbox.

Enforces ``fs_allow/deny`` via path-prefix check. Does NOT enforce network policy
(its ``capabilities().network_egress_control`` is False so the assembler will reject
configs requiring egress control).

This is a developer-friendly backend. Production consumers should plug a real
sandbox (Modal, OpenShell, etc.).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from ..errors import SandboxDenied
from .policy import Policy
from .protocol import ExecuteResult, FileInfo, SandboxBackend, SandboxCapabilities, WriteResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    # BEGIN harness-extensibility story 6
    from ..extensions.base import SecretResolver
    # END harness-extensibility story 6


class LocalShellSandbox(SandboxBackend):
    name = "local_shell"

    def __init__(
        self,
        *,
        max_output_bytes: int = 5 * 1024 * 1024,
        # BEGIN harness-extensibility story 6
        resolver: SecretResolver | None = None,
        # END harness-extensibility story 6
    ) -> None:
        self.max_output_bytes = max_output_bytes
        # BEGIN harness-extensibility story 6
        self._resolver = resolver
        # END harness-extensibility story 6

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            filesystem_isolation=False,
            network_egress_control=False,
            seccomp=False,
            landlock=False,
            secret_handle_deref=True,
        )

    async def execute(
        self,
        cmd: Sequence[str],
        *,
        policy: Policy,
        cwd: str | None = None,
        stdin: bytes | None = None,
    ) -> ExecuteResult:
        if cwd is not None and not policy.check_fs(cwd):
            raise SandboxDenied("fs policy denied cwd", resource=cwd)
        env = await self._build_env(policy)
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(input=stdin), timeout=policy.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                duration_ms = int((time.monotonic() - start) * 1000)
                return ExecuteResult(exit_code=124, stdout="", stderr="timeout", duration_ms=duration_ms, truncated=False)
        except FileNotFoundError as exc:
            raise SandboxDenied(f"executable not found: {cmd[0]!r}", resource=cmd[0]) from exc

        truncated = False
        if len(out_b) > self.max_output_bytes:
            out_b = out_b[: self.max_output_bytes]
            truncated = True
        if len(err_b) > self.max_output_bytes:
            err_b = err_b[: self.max_output_bytes]
            truncated = True
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecuteResult(
            exit_code=int(proc.returncode or 0),
            stdout=out_b.decode("utf-8", errors="replace"),
            stderr=err_b.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            truncated=truncated,
        )

    async def read_file(self, path: str, *, policy: Policy) -> bytes:
        if not policy.check_fs(path):
            raise SandboxDenied("fs policy denied read", resource=path)
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        except FileNotFoundError as exc:
            raise SandboxDenied("file not found", resource=path) from exc

    async def write_file(self, path: str, content: bytes, *, policy: Policy) -> WriteResult:
        if not policy.check_fs(path):
            raise SandboxDenied("fs policy denied write", resource=path)
        p = Path(path)
        await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, content)
        return WriteResult(ok=True, path=str(p), bytes_written=len(content))

    async def list_files(self, path: str, *, policy: Policy) -> list[FileInfo]:
        if not policy.check_fs(path):
            raise SandboxDenied("fs policy denied list", resource=path)
        p = Path(path)
        if not p.exists():
            raise SandboxDenied("path not found", resource=path)
        out: list[FileInfo] = []
        for child in await asyncio.to_thread(list, p.iterdir()):
            st = child.stat()
            out.append(
                FileInfo(
                    path=str(child),
                    size=st.st_size,
                    is_dir=child.is_dir(),
                    modified_time=st.st_mtime,
                )
            )
        return out

    def _filtered_env(self, policy: Policy) -> dict[str, str]:
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for key in policy.env_allow:
            if key in os.environ:
                base[key] = os.environ[key]
        for name, handle in policy.secret_handles.items():
            if handle in os.environ:
                base[name] = os.environ[handle]
        return base

    # BEGIN harness-extensibility story 6
    async def _build_env(self, policy: Policy) -> dict[str, str]:
        """Compose the child-process env: PATH + env_allow + resolved secrets.

        When a :class:`SecretResolver` is configured the resolver-based
        ``Policy.materialize`` path is used; otherwise fall back to the
        legacy ``_filtered_env`` behaviour so pre-Story-6 call sites that
        never configure a resolver keep working.
        """
        if self._resolver is None:
            return self._filtered_env(policy)
        base: dict[str, str] = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for key in policy.env_allow:
            if key in os.environ:
                base[key] = os.environ[key]
        secrets = await policy.materialize(self._resolver)
        base.update(secrets)
        return base
    # END harness-extensibility story 6
