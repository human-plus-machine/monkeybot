"""Tests for the HarnessConfig sandbox adapter used by DeepAgents."""

from __future__ import annotations

from collections.abc import Sequence

from src.core.harness.sandbox.deepagents_backend import HarnessDeepAgentsSandbox
from src.core.harness.sandbox.policy import Policy
from src.core.harness.sandbox.protocol import (
    ExecuteResult,
    FileInfo,
    SandboxCapabilities,
    WriteResult,
)


class RecordingSandbox:
    name = "recording"

    def __init__(self) -> None:
        self.execute_calls: list[tuple[Sequence[str], Policy]] = []

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities()

    async def execute(
        self,
        cmd: Sequence[str],
        *,
        policy: Policy,
        cwd: str | None = None,
        stdin: bytes | None = None,
    ) -> ExecuteResult:
        self.execute_calls.append((cmd, policy))
        return ExecuteResult(
            exit_code=7,
            stdout="stdout",
            stderr="stderr",
            duration_ms=12,
            truncated=True,
        )

    async def read_file(self, path: str, *, policy: Policy) -> bytes:
        return b"contents"

    async def write_file(
        self,
        path: str,
        content: bytes,
        *,
        policy: Policy,
    ) -> WriteResult:
        return WriteResult(ok=True, path=path, bytes_written=len(content))

    async def list_files(self, path: str, *, policy: Policy) -> list[FileInfo]:
        return []


def test_harness_deepagents_sandbox_execute_maps_result() -> None:
    sandbox = RecordingSandbox()
    policy = Policy(
        fs_allow=(),
        fs_deny=(),
        net_allow=(),
        net_deny=(),
        env_allow=(),
        secret_handles={},
        timeout_seconds=300,
    )

    backend = HarnessDeepAgentsSandbox(sandbox, policy)

    result = backend.execute("printf hi", timeout=5)

    assert result.output == "stdout\nstderr"
    assert result.exit_code == 7
    assert result.truncated is True
    assert sandbox.execute_calls == [
        (
            ["/bin/sh", "-c", "printf hi"],
            Policy(
                fs_allow=(),
                fs_deny=(),
                net_allow=(),
                net_deny=(),
                env_allow=(),
                secret_handles={},
                timeout_seconds=5,
            ),
        )
    ]


def test_harness_deepagents_sandbox_file_transfer_delegates_to_sandbox() -> None:
    sandbox = RecordingSandbox()
    policy = Policy(
        fs_allow=(),
        fs_deny=(),
        net_allow=(),
        net_deny=(),
        env_allow=(),
        secret_handles={},
        timeout_seconds=300,
    )

    backend = HarnessDeepAgentsSandbox(sandbox, policy)

    upload_response = backend.upload_files([("/tmp/data.txt", b"contents")])
    download_response = backend.download_files(["/tmp/data.txt"])

    assert [(r.path, r.error) for r in upload_response] == [
        ("/tmp/data.txt", None)
    ]
    assert [(r.path, r.content, r.error) for r in download_response] == [
        ("/tmp/data.txt", b"contents", None)
    ]
