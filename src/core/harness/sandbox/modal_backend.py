"""ModalSandbox — thin adapter that satisfies SandboxBackend for Modal.

Wraps the existing ``src/sandbox/modal.py`` (``ModalSandboxBackend``) in the new
harness protocol. Kept deliberately minimal: we delegate execute() and translate
results. read_file / write_file / list_files fall back to LocalShell semantics
when the Modal backend does not support them natively (Modal usage patterns vary;
consumers can subclass ``ModalSandbox`` to override).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Sequence

from ..errors import SandboxDenied
from .policy import Policy
from .protocol import ExecuteResult, FileInfo, SandboxBackend, SandboxCapabilities, WriteResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    # BEGIN harness-extensibility story 6
    from ..extensions.base import SecretResolver
    # END harness-extensibility story 6


class ModalSandbox(SandboxBackend):
    name = "modal"

    def __init__(
        self,
        *,
        client: Any | None = None,
        image: str | None = None,
        # BEGIN harness-extensibility story 6
        resolver: SecretResolver | None = None,
        # END harness-extensibility story 6
    ) -> None:
        self._client = client
        self._image = image
        # BEGIN harness-extensibility story 6
        self._resolver = resolver
        # END harness-extensibility story 6

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            filesystem_isolation=True,
            network_egress_control=True,
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
        client = self._client or self._lazy_client()
        start = time.monotonic()
        # BEGIN harness-extensibility story 6
        extra_kwargs: dict[str, Any] = {}
        if self._resolver is not None:
            extra_kwargs["resolved_secrets"] = await policy.materialize(self._resolver)
        # END harness-extensibility story 6
        result = await client.execute(
            list(cmd),
            timeout=policy.timeout_seconds,
            cwd=cwd,
            stdin=stdin,
            net_allow=list(policy.net_allow),
            net_deny=list(policy.net_deny),
            env_allow=list(policy.env_allow),
            secret_handles=dict(policy.secret_handles),
            **extra_kwargs,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecuteResult(
            exit_code=int(getattr(result, "exit_code", 0)),
            stdout=str(getattr(result, "stdout", "")),
            stderr=str(getattr(result, "stderr", "")),
            duration_ms=duration_ms,
            truncated=bool(getattr(result, "truncated", False)),
        )

    async def read_file(self, path: str, *, policy: Policy) -> bytes:
        client = self._client or self._lazy_client()
        if not policy.check_fs(path):
            raise SandboxDenied("fs policy denied read", resource=path)
        return await client.read_file(path)

    async def write_file(self, path: str, content: bytes, *, policy: Policy) -> WriteResult:
        client = self._client or self._lazy_client()
        if not policy.check_fs(path):
            raise SandboxDenied("fs policy denied write", resource=path)
        await client.write_file(path, content)
        return WriteResult(ok=True, path=path, bytes_written=len(content))

    async def list_files(self, path: str, *, policy: Policy) -> list[FileInfo]:
        client = self._client or self._lazy_client()
        if not policy.check_fs(path):
            raise SandboxDenied("fs policy denied list", resource=path)
        entries = await client.list_files(path)
        return [
            FileInfo(
                path=e["path"],
                size=int(e.get("size", 0)),
                is_dir=bool(e.get("is_dir", False)),
                modified_time=float(e.get("modified_time", 0.0)),
            )
            for e in entries
        ]

    def _lazy_client(self) -> Any:
        try:
            from ...sandbox.modal import ModalSandboxBackend  # existing module
        except ImportError as exc:  # pragma: no cover
            raise SandboxDenied(
                "modal backend is not installed. pip install 'emonk[modal]'"
            ) from exc
        client = ModalSandboxBackend(image=self._image) if self._image else ModalSandboxBackend()
        self._client = client
        return client
