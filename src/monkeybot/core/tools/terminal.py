"""
Secure terminal command execution with allowlist-based security.

This module provides the TerminalExecutor class, which is the security boundary
for all shell command execution in Monkeybot. It implements strict allowlist validation
for both commands and file paths to prevent unauthorized operations.

Security Model:
    - Deny by default: Only pre-approved commands and paths are allowed
    - Command allowlist: ALLOWED_COMMANDS defines executable binaries
    - Path allowlist: ALLOWED_PATHS defines accessible directories
    - Timeout enforcement: All commands have maximum execution time
    - Output limits: Large outputs are truncated to prevent memory exhaustion

Example:
    >>> executor = TerminalExecutor()
    >>> result = await executor.execute("ls", ["../memory/"])
    >>> print(result.stdout)
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# After the child exits or is killed, wait at most this long for stdout/stderr EOF.
# Descendants that keep pipes open must not block execute() forever.
_STREAM_DRAIN_TIMEOUT_SEC = 2.0

# SECURITY: Command allowlist - modify with extreme caution
# Only add commands that are essential and have been security reviewed
ALLOWED_COMMANDS = [
    "cat",      # Read file contents
    "ls",       # List directory contents
    "grep",     # Search file contents (line-oriented)
    "rg",       # ripgrep — fast content search (prefer builtin grep tool when available)
    "echo",     # Print text (used in tests and debugging)
    "python",   # Execute Python scripts (skills)
    "python3",  # Execute Python scripts (skills)
    "uv",       # Package runner (install subcommands blocked by deny_patterns)
    "git",      # Version control (clone, branch, commit, push, etc.)
    "gh",       # GitHub CLI (e.g. gh pr create)
    "bash",     # Shell interpreter — use for builtins (cd, &&, pipes) via bash -c "..."
]

# SECURITY: Path allowlist - modify with extreme caution
# Only add paths that are safe for agent access
ALLOWED_PATHS = [
    "../memory/",        # Agent-home memory (cwd is workspace/)
    "../memory",         # Same, when callers omit trailing slash
    "./skills/",         # Skills directory
    "./skills",          # Same, when callers omit trailing slash
    "./global-skills/",  # Shared library authoring (Main Agent Studio)
    "./global-skills",   # Same, when callers omit trailing slash
    "./test-data/",      # Test data directory (for tests only)
    "./code/",           # Reference / cloned repos (explicit ./ paths in argv)
    "./code",            # Same, when callers omit trailing slash
]


_VERTEX_SKILL_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "VERTEX_AI_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "GCP_PROJECT_ID",
    "VERTEX_AI_LOCATION",
    "GOOGLE_CLOUD_REGION",
)


def build_skill_runtime_env(*, cwd: Path | str) -> dict[str, str]:
    """Environment for skill scripts (inherits host env + workspace/GCP overrides).

    Prepends the gateway interpreter's ``bin`` directory to ``PATH`` so
    ``bash -c 'python3 …'`` resolves the same Python that has monkeybot
    extras (e.g. ``google-genai``), not a bare system ``python3``.
    """
    exec_cwd = str(Path(cwd).resolve())
    env = os.environ.copy()
    env["MONKEYBOT_WORKSPACE_ROOT"] = exec_cwd
    env["WORKSPACE_ROOT"] = exec_cwd
    venv_bin = str(Path(sys.executable).resolve().parent)
    existing_path = env.get("PATH", "")
    env["PATH"] = f"{venv_bin}{os.pathsep}{existing_path}" if existing_path else venv_bin
    env.setdefault("MONKEYBOT_PYTHON", sys.executable)
    for key in _VERTEX_SKILL_ENV_KEYS:
        val = os.environ.get(key, "").strip()
        if val:
            env[key] = val
    for cred_env in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_AUTH_FILE"):
        raw = os.environ.get(cred_env, "").strip()
        if not raw:
            continue
        cred_path = Path(raw).expanduser()
        if not cred_path.is_absolute():
            cred_path = (Path.cwd() / cred_path).resolve()
        else:
            cred_path = cred_path.resolve()
        env[cred_env] = str(cred_path)
    return env


@dataclass
class ExecutionResult:
    """
    Result from terminal command execution.
    
    Attributes:
        stdout: Standard output from command (decoded UTF-8)
        stderr: Standard error from command (decoded UTF-8)
        exit_code: Command exit code (0 = success, non-zero = error)
    """
    stdout: str
    stderr: str
    exit_code: int


class SecurityError(Exception):
    """
    Raised when a security violation is detected.
    
    This exception is raised when attempting to execute:
    - A command not in ALLOWED_COMMANDS
    - A command accessing paths not in ALLOWED_PATHS
    
    Security violations are logged with ERROR severity for audit purposes.
    """
    pass


class CommandTimeoutError(TimeoutError):
    """Raised when a command exceeds its timeout; carries drained partial streams.

    ``stdout`` / ``stderr`` are whatever was emitted before the process was
    killed (may be empty). Callers should spill these rather than dumping them
    into the model context.
    """

    def __init__(
        self,
        message: str,
        *,
        timeout: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr


async def _pump_stream(stream: asyncio.StreamReader, chunks: list[bytes]) -> None:
    """Append stream chunks until EOF; safe to cancel (keeps chunks already read)."""
    while True:
        data = await stream.read(65536)
        if not data:
            return
        chunks.append(data)


async def _drain_pumps(
    *tasks: asyncio.Task[None],
    timeout: float = _STREAM_DRAIN_TIMEOUT_SEC,
) -> bool:
    """Wait for stream pumps to finish; cancel stragglers after ``timeout``.

    Returns True when every pump finished cleanly (EOF), False if any were cancelled.
    """
    if not tasks:
        return True
    _done, pending = await asyncio.wait(set(tasks), timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "stream drain timed out; cancelled %d reader(s)",
            len(pending),
            extra={"component": "terminal_executor", "pending_readers": len(pending)},
        )
        return False
    return True


async def _wait_for_exit(process: asyncio.subprocess.Process, timeout: float) -> bool:
    """Wait until the direct child exits.

    Returns True if still running after ``timeout``.

    Polls ``returncode`` instead of awaiting ``process.wait()`` alone: when a
    descendant keeps stdout/stderr open, asyncio can set ``returncode`` while
    ``wait()`` never completes.
    """
    if process.returncode is not None:
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    wait_task = asyncio.create_task(process.wait())
    try:
        while process.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            await asyncio.wait({wait_task}, timeout=min(remaining, 0.1))
            if wait_task.done() and process.returncode is None:
                # wait() finished exceptionally; surface it
                await wait_task
        return False
    finally:
        if not wait_task.done():
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task


def _kill_process_group(
    pgid: int | None,
    process: asyncio.subprocess.Process,
) -> None:
    """SIGKILL ``pgid`` when known so pipe-holding descendants die too.

    ``pgid`` must be captured before the direct child is reaped — after
    ``wait()`` the PID may be gone and ``getpgid`` will fail.
    """
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


class TerminalExecutor:
    """
    Secure terminal command executor with allowlist-based security.
    
    This class is the security boundary for all shell command execution in Monkeybot.
    It enforces strict allowlist validation for commands and file paths, implements
    timeout handling, and limits output size to prevent resource exhaustion.
    
    Security Features:
        - Command allowlist validation (ALLOWED_COMMANDS)
        - Path allowlist validation (ALLOWED_PATHS)
        - Timeout enforcement with process cleanup
        - Output size limits (1MB per stream)
        - Security violation logging
    
    Example:
        >>> executor = TerminalExecutor()
        >>> 
        >>> # Allowed command + allowed path - succeeds
        >>> result = await executor.execute("cat", ["../memory/file.txt"])
        >>> print(result.stdout)
        >>> 
        >>> # Blocked command - raises SecurityError
        >>> try:
        >>>     await executor.execute("rm", ["-rf", "/"])
        >>> except SecurityError as e:
        >>>     print(f"Blocked: {e}")
    """

    def __init__(
        self,
        *,
        allowed_commands: Sequence[str] | None = None,
        allowed_path_prefixes: Sequence[str] | None = None,
    ) -> None:
        self._allowed_commands: tuple[str, ...] = (
            tuple(allowed_commands) if allowed_commands is not None else tuple(ALLOWED_COMMANDS)
        )
        self._allowed_path_prefixes: tuple[str, ...] = (
            tuple(allowed_path_prefixes)
            if allowed_path_prefixes is not None
            else tuple(ALLOWED_PATHS)
        )

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        return self._allowed_commands

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        return self._allowed_path_prefixes

    async def aclose(self) -> None:
        """No-op — TerminalExecutor holds no persistent resources."""

    async def execute(
        self,
        command: str,
        args: List[str],
        timeout: int = 60,
        *,
        cwd: Path | str | None = None,
    ) -> ExecutionResult:
        """
        Execute a terminal command securely with allowlist validation.
        
        This method is the single entry point for all shell command execution.
        It performs security validation before execution and enforces resource
        limits during execution.
        
        Args:
            command: Command to execute (must be in ALLOWED_COMMANDS)
            args: Command arguments (paths must be in ALLOWED_PATHS)
            timeout: Maximum execution time in seconds (default: 60)
        
        Returns:
            ExecutionResult containing stdout, stderr, and exit code
        
        Raises:
            SecurityError: If command or path violates security policy
            CommandTimeoutError: If command exceeds timeout duration (includes
                drained partial stdout/stderr)
        
        Example:
            >>> executor = TerminalExecutor()
            >>> result = await executor.execute("ls", ["-la", "../memory/"])
            >>> if result.exit_code == 0:
            >>>     print(f"Files: {result.stdout}")
        
        Security Notes:
            - This method logs all security violations with ERROR severity
            - Failed security checks never execute the command
            - Processes are killed if they exceed timeout
            - Output is truncated if it exceeds 1MB per stream
        """
        # CRITICAL: Validate command against allowlist
        self._validate_command(command)
        
        # CRITICAL: Validate all paths in arguments
        self._validate_paths(args)
        
        # Log execution for audit trail
        logger.info(
            f"Executing command: {command} {' '.join(args)}",
            extra={
                "component": "terminal_executor",
                "command": command,
                "args_count": len(args)
            }
        )
        
        exec_cwd: str | None = None
        env: dict[str, str] | None = None
        if cwd is not None:
            exec_cwd = str(Path(cwd).resolve())
            env = build_skill_runtime_env(cwd=exec_cwd)

        executable = sys.executable if command in ("python3", "python") else command

        # Concurrent stream pumps avoid PIPE-buffer deadlock while we wait on
        # the process, and preserve partial output when we kill on timeout.
        # start_new_session=True makes the child a process-group leader so we
        # can kill descendants that would otherwise keep capture pipes open.
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_cwd,
            env=env,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            pgid: int | None = os.getpgid(process.pid) if process.pid is not None else None
        except ProcessLookupError:
            pgid = process.pid
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_task = asyncio.create_task(_pump_stream(process.stdout, stdout_chunks))
        stderr_task = asyncio.create_task(_pump_stream(process.stderr, stderr_chunks))
        timed_out = False
        if await _wait_for_exit(process, timeout):
            timed_out = True
            _kill_process_group(pgid, process)
            # Reap the direct child if it is still listed; ignore if already gone.
            with contextlib.suppress(ProcessLookupError):
                await _wait_for_exit(process, _STREAM_DRAIN_TIMEOUT_SEC)

        drained_clean = await _drain_pumps(stdout_task, stderr_task)
        if not drained_clean:
            # Orphans may still hold the capture pipes open after the child exits.
            _kill_process_group(pgid, process)

        stdout_raw = self._truncate_output(b"".join(stdout_chunks), "stdout")
        stderr_raw = self._truncate_output(b"".join(stderr_chunks), "stderr")
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")

        if timed_out:
            error_msg = f"Command exceeded {timeout}s timeout"
            logger.error(
                error_msg,
                extra={
                    "component": "terminal_executor",
                    "command": command,
                    "timeout": timeout,
                    "stdout_chars": len(stdout),
                    "stderr_chars": len(stderr),
                },
            )
            raise CommandTimeoutError(
                error_msg,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
            )

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode or 0,
        )
    
    def _validate_command(self, command: str) -> None:
        """
        Validate command against allowlist.
        
        Args:
            command: Command to validate
        
        Raises:
            SecurityError: If command is not in ALLOWED_COMMANDS
        """
        if command not in self._allowed_commands:
            error_msg = f"Command '{command}' not allowed"
            logger.error(
                f"Security violation: {error_msg}",
                extra={
                    "component": "terminal_executor",
                    "severity": "SECURITY_VIOLATION",
                    "command": command,
                    "allowed_commands": list(self._allowed_commands),
                },
            )
            raise SecurityError(error_msg)
    
    def _validate_paths(self, args: List[str]) -> None:
        """
        Validate file paths in arguments against allowlist.
        
        This method checks each argument to see if it looks like a file path
        (starts with "./" or "/"). If so, it validates that the path starts
        with one of the allowed path prefixes.
        
        Args:
            args: Command arguments to validate
        
        Raises:
            SecurityError: If any path argument is not in ALLOWED_PATHS
        
        Security Notes:
            - Uses startswith() to allow subdirectories of allowed paths
            - Empty args list is allowed (no paths to validate)
            - Non-path arguments (flags, values) are ignored
            - Absolute paths starting with /tmp or /var/folders are allowed (for tests)
        """
        from pathlib import Path
        
        for arg in args:
            # Check if this argument is a file path
            if arg.startswith("./") or arg.startswith("/"):
                # Allow test directories (pytest tmp_path)
                # macOS: /private/var/folders/, Linux: /tmp/
                if (arg.startswith("/tmp/") or 
                    arg.startswith("/var/folders/") or 
                    arg.startswith("/private/var/folders/")):
                    continue
                
                # Validate path is in allowed directories
                if not any(arg.startswith(allowed) for allowed in self._allowed_path_prefixes):
                    error_msg = f"Path '{arg}' not allowed"
                    logger.error(
                        f"Security violation: {error_msg}",
                        extra={
                            "component": "terminal_executor",
                            "severity": "SECURITY_VIOLATION",
                            "path": arg,
                            "allowed_paths": list(self._allowed_path_prefixes),
                        },
                    )
                    raise SecurityError(error_msg)
    
    def _truncate_output(self, output: bytes, stream_name: str) -> bytes:
        """
        Truncate large output to prevent memory exhaustion.
        
        Args:
            output: Raw output bytes from subprocess
            stream_name: Name of stream for logging ("stdout" or "stderr")
        
        Returns:
            Truncated output bytes (original if under limit)
        
        Notes:
            - Maximum output size: 1MB per stream
            - Truncated outputs include warning message
            - Truncation is logged at WARNING level
        """
        MAX_OUTPUT_SIZE = 1024 * 1024  # 1MB
        
        if len(output) > MAX_OUTPUT_SIZE:
            logger.warning(
                f"Truncating {stream_name}: {len(output)} bytes -> {MAX_OUTPUT_SIZE} bytes",
                extra={
                    "component": "terminal_executor",
                    "stream": stream_name,
                    "original_size": len(output),
                    "truncated_size": MAX_OUTPUT_SIZE
                }
            )
            return output[:MAX_OUTPUT_SIZE] + b"\n[Output truncated at 1MB limit]"
        
        return output
