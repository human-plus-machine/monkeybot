"""Monkey-patch deepagents FilesystemMiddleware before build_deep_agent.

- Removes built-in file tools (ls, read_file, write_file, edit_file, glob, grep); keeps execute.
- Hardens execute against empty command (recoverable error instead of validation failure).

Import and call ``apply_deepagents_patches()`` once at process startup (no secrets/GCP).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import deepagents.middleware.filesystem as _deepagents_fs
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from pydantic import Field

_applied = False
_logger = logging.getLogger(__name__)


def _execute_empty_command_response(command: str) -> str:
    """Return tool-visible error and log where validation ran (no shell process is started)."""
    validator = Path(__file__).resolve()
    received = repr(command)
    if len(received) > 400:
        received = received[:397] + "..."
    _logger.warning(
        "execute tool rejected empty command (validator=%s, command_received=%s)",
        str(validator),
        received,
        extra={
            "event": "execute_empty_command",
            "source": str(validator),
            "command_received": received,
        },
    )
    example = 'execute(command="python3 $APP_ROOT/skills/example/script.py --help")'
    return (
        "Error: execute requires a non-empty `command` string. "
        "No subprocess was started (nothing to run). "
        f"Received command={received}. "
        f"Empty-command validation: {validator}\n\n"
        "Pass the full shell line as command=..., for example:\n"
        f"{example}\n"
        "This tool does not accept a separate timeout parameter — only command."
    )


def _create_execute_tool_command_default(self):  # type: ignore[no-untyped-def]
    tool_description = self._custom_tool_descriptions.get("execute") or _deepagents_fs.EXECUTE_TOOL_DESCRIPTION
    supports = _deepagents_fs._supports_execution

    def sync_execute(
        runtime: ToolRuntime[None, _deepagents_fs.FilesystemState],
        command: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Full shell command (required). Example: python3 $APP_ROOT/skills/.../script.py --help"
                ),
            ),
        ],
    ) -> str:
        if not (command or "").strip():
            return _execute_empty_command_response(command)
        resolved_backend = self._get_backend(runtime)
        if not supports(resolved_backend):
            return (
                "Error: Execution not available. This agent's backend "
                "does not support command execution (SandboxBackendProtocol). "
                "To use the execute tool, provide a backend that implements SandboxBackendProtocol."
            )
        try:
            result = resolved_backend.execute(command)
        except NotImplementedError as e:
            return f"Error: Execution not available. {e}"
        parts = [result.output]
        if result.exit_code is not None:
            status = "succeeded" if result.exit_code == 0 else "failed"
            parts.append(f"\n[Command {status} with exit code {result.exit_code}]")
        if result.truncated:
            parts.append("\n[Output was truncated due to size limits]")
        return "".join(parts)

    async def async_execute(
        runtime: ToolRuntime[None, _deepagents_fs.FilesystemState],
        command: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Full shell command (required). Example: python3 $APP_ROOT/skills/.../script.py --help"
                ),
            ),
        ],
    ) -> str:
        if not (command or "").strip():
            return _execute_empty_command_response(command)
        resolved_backend = self._get_backend(runtime)
        if not supports(resolved_backend):
            return (
                "Error: Execution not available. This agent's backend "
                "does not support command execution (SandboxBackendProtocol). "
                "To use the execute tool, provide a backend that implements SandboxBackendProtocol."
            )
        try:
            result = await resolved_backend.aexecute(command)
        except NotImplementedError as e:
            return f"Error: Execution not available. {e}"
        parts = [result.output]
        if result.exit_code is not None:
            status = "succeeded" if result.exit_code == 0 else "failed"
            parts.append(f"\n[Command {status} with exit code {result.exit_code}]")
        if result.truncated:
            parts.append("\n[Output was truncated due to size limits]")
        return "".join(parts)

    return StructuredTool.from_function(
        name="execute",
        description=tool_description,
        func=sync_execute,
        coroutine=async_execute,
    )


def apply_deepagents_patches() -> None:
    """Idempotent: safe to call multiple times (second+ is no-op)."""
    global _applied
    if _applied:
        return
    _applied = True

    _orig_init = _deepagents_fs.FilesystemMiddleware.__init__

    def _filesystem_middleware_init_execute_only(self, **kwargs):  # type: ignore[no-untyped-def]
        _orig_init(self, **kwargs)
        self.tools = [t for t in self.tools if getattr(t, "name", None) == "execute"]

    _deepagents_fs.FilesystemMiddleware.__init__ = _filesystem_middleware_init_execute_only

    _execute_desc = _deepagents_fs.EXECUTE_TOOL_DESCRIPTION
    _deepagents_fs.EXECUTE_TOOL_DESCRIPTION = (
        "REQUIRED parameter: `command` — a non-empty shell string. "
        "Every execute call MUST include `command` (e.g. execute(command=\"python3 script.py\")). "
        "Never invoke execute with empty arguments. "
        "This tool does not accept a separate `timeout` argument; only `command` is supported.\n\n"
        + _execute_desc
    )

    _deepagents_fs.FilesystemMiddleware._create_execute_tool = _create_execute_tool_command_default
