"""SandboxBackend Protocol + capability + result dataclasses.

Capability flags let the assembler reject a PolicySpec that demands controls the
chosen backend does not enforce (prevents silent-downgrade).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from .policy import Policy


@dataclass(frozen=True)
class ExecuteResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    path: str
    bytes_written: int


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int
    is_dir: bool
    modified_time: float


@dataclass(frozen=True)
class SandboxCapabilities:
    filesystem_isolation: bool = False
    network_egress_control: bool = False
    seccomp: bool = False
    landlock: bool = False
    secret_handle_deref: bool = False


@runtime_checkable
class SandboxBackend(Protocol):
    name: str

    def capabilities(self) -> SandboxCapabilities: ...

    async def execute(
        self,
        cmd: Sequence[str],
        *,
        policy: Policy,
        cwd: str | None = None,
        stdin: bytes | None = None,
    ) -> ExecuteResult: ...

    async def read_file(self, path: str, *, policy: Policy) -> bytes: ...

    async def write_file(self, path: str, content: bytes, *, policy: Policy) -> WriteResult: ...

    async def list_files(self, path: str, *, policy: Policy) -> list[FileInfo]: ...
