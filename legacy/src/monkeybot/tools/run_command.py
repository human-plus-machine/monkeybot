"""
run_command — the master tool.
Security: every command passes through the ToolInspector chain first.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


TOOL_DEF = {
    "name": "run_command",
    "description": (
        "Execute a shell command or Python script. "
        "Use this to run skills, call CLIs, or execute any deterministic operation. "
        "Prefer specific scripts over raw shell when possible."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute"},
            "working_dir": {"type": "string", "description": "Working directory (optional)"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
        },
        "required": ["command"],
    },
}

_REDACTED_VARS = {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DB_URL"}


def _safe_env() -> dict[str, str]:
    """Return os.environ copy with known secret vars redacted."""
    env = os.environ.copy()
    for key in _REDACTED_VARS:
        if key in env:
            env[key] = "REDACTED"
    return env


async def run_command(
    command: str,
    working_dir: str | None = None,
    timeout: int = 30,
) -> CommandResult:
    """Execute shell command, return CommandResult."""
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=_safe_env(),
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResult(
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            exit_code=proc.returncode or 0,
            duration_ms=duration_ms,
        )
    except TimeoutError:
        return CommandResult(
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            exit_code=124,
            duration_ms=timeout * 1000,
        )


def format_result(r: CommandResult) -> str:
    """Format CommandResult for model consumption."""
    parts = [f"exit_code: {r.exit_code}", f"stdout: {r.stdout}"]
    if r.stderr:
        parts.append(f"stderr: {r.stderr}")
    parts.append(f"duration_ms: {r.duration_ms}")
    return "\n".join(parts)
