"""Adapter from the harness sandbox protocol to DeepAgents' sandbox backend."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from uuid import uuid4

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from ..errors import SandboxDenied
from .policy import Policy
from .protocol import ExecuteResult, SandboxBackend


class HarnessDeepAgentsSandbox(BaseSandbox):
    """DeepAgents backend that delegates execution and file I/O to a harness sandbox."""

    def __init__(self, sandbox: SandboxBackend, policy: Policy) -> None:
        self._sandbox = sandbox
        self._policy = policy
        self._id = f"{sandbox.name}-{uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        return self._id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        result = cast(ExecuteResult, _run_sync(self._execute(command, timeout=timeout)))
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        return ExecuteResponse(
            output=output,
            exit_code=result.exit_code,
            truncated=result.truncated,
        )

    async def _execute(self, command: str, *, timeout: int | None) -> Any:
        policy = self._policy
        if timeout is not None and timeout != policy.timeout_seconds:
            policy = Policy(
                fs_allow=policy.fs_allow,
                fs_deny=policy.fs_deny,
                net_allow=policy.net_allow,
                net_deny=policy.net_deny,
                env_allow=policy.env_allow,
                secret_handles=policy.secret_handles,
                timeout_seconds=timeout,
            )
        return await self._sandbox.execute(
            ["/bin/sh", "-c", command],
            policy=policy,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return cast(list[FileUploadResponse], _run_sync(self._upload_files(files)))

    async def _upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                result = await self._sandbox.write_file(
                    path,
                    content,
                    policy=self._policy,
                )
                responses.append(
                    FileUploadResponse(path=result.path, error=None)
                )
            except SandboxDenied:
                responses.append(
                    FileUploadResponse(path=path, error="permission_denied")
                )
            except Exception:  # noqa: BLE001 - protocol reports per-file errors
                responses.append(
                    FileUploadResponse(path=path, error="invalid_path")
                )
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return cast(list[FileDownloadResponse], _run_sync(self._download_files(paths)))

    async def _download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = await self._sandbox.read_file(path, policy=self._policy)
                responses.append(
                    FileDownloadResponse(path=path, content=content, error=None)
                )
            except SandboxDenied:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=None,
                        error="permission_denied",
                    )
                )
            except Exception:  # noqa: BLE001 - protocol reports per-file errors
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="invalid_path")
                )
        return responses


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()
