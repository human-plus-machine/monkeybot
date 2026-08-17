"""
Comprehensive tests for Terminal Executor.

This test suite ensures 100% coverage of security-critical paths in the
Terminal Executor, including:
    - Command allowlist validation
    - Path allowlist validation
    - Timeout handling with process cleanup
    - Output truncation
    - Error handling

Security tests are marked with SECURITY comment for easy identification.
"""

import asyncio

import pytest

from monkeybot.core.tools import terminal as terminal_module
from monkeybot.core.tools.fs_isolation import (
    ISOLATION_ERROR_PREFIX,
    ISOLATION_FAILURE_EXIT_CODE,
    IsolationSupport,
    isolation_support,
)
from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    ExecutionResult,
    SecurityError,
    TerminalExecutor,
    build_skill_runtime_env,
)


@pytest.fixture
def executor():
    """Create a terminal executor instance for testing."""
    return TerminalExecutor()


@pytest.fixture
def test_data_dir(tmp_path):
    """
    Create temporary test data directory.
    
    Creates a directory structure that mimics the allowed paths:
        tmp_path/
            data/
                memory/
                    test.txt
    """
    data_dir = tmp_path / "data" / "memory"
    data_dir.mkdir(parents=True)

    # Create a test file
    test_file = data_dir / "test.txt"
    test_file.write_text("Hello from test file")

    return tmp_path


class TestTerminalExecutorSecurity:
    """
    Security-focused tests for Terminal Executor.
    
    These tests verify that the security boundary is properly enforced.
    ALL tests in this class must pass for the system to be secure.
    """

    @pytest.mark.asyncio
    async def test_allowed_command_succeeds(self, executor):
        """SECURITY: Test that allowed command executes successfully."""
        result = await executor.execute("echo", ["Hello World"])

        assert isinstance(result, ExecutionResult)
        assert result.exit_code == 0
        assert "Hello World" in result.stdout
        assert result.stderr == ""

    @pytest.mark.asyncio
    async def test_blocked_command_raises_security_error(self, executor):
        """SECURITY: Test that blocked command raises SecurityError."""
        # Test various dangerous commands
        dangerous_commands = ["rm", "curl", "wget", "sudo", "chmod", "chown"]

        for cmd in dangerous_commands:
            with pytest.raises(SecurityError) as exc_info:
                await executor.execute(cmd, ["-rf", "/"])

            assert "not allowed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_allowed_path_succeeds(self, executor, test_data_dir):
        """SECURITY: Test that command with allowed path succeeds."""
        # Create test file in allowed directory (using test-data which is in ALLOWED_PATHS)
        test_file = test_data_dir / "test-data" / "test.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("Hello from allowed path")

        result = await executor.execute("cat", [str(test_file)], cwd=test_data_dir)

        assert result.exit_code == 0
        assert "Hello from allowed path" in result.stdout

    @pytest.mark.asyncio
    async def test_blocked_path_raises_security_error(self, executor):
        """SECURITY: Test that command with blocked path raises SecurityError."""
        # Test various system paths that should be blocked
        dangerous_paths = [
            "/etc/passwd",
            "/etc/shadow",
            "/root/.ssh/id_rsa",
            "/var/log/system.log",
            "./unauthorized/path/file.txt"
        ]

        for path in dangerous_paths:
            with pytest.raises(SecurityError) as exc_info:
                await executor.execute("cat", [path])

            assert "not allowed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, executor):
        """SECURITY: Test that path traversal attempts are blocked."""
        # Attempt to use path traversal to access unauthorized files
        # These paths start with allowed prefixes but try to escape
        traversal_attempts = [
            "./unauthorized/path/file.txt",  # Not in allowed paths
            "/etc/passwd",                     # System file
        ]

        # These should be blocked by the allowlist check
        for path in traversal_attempts:
            with pytest.raises(SecurityError) as exc_info:
                await executor.execute("cat", [path])

            assert "not allowed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_allowed_prefix_cannot_be_escaped_with_dot_dot(self, executor, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "skills").mkdir(parents=True)
        secret = workspace / "secret.txt"
        secret.write_text("secret", encoding="utf-8")

        with pytest.raises(SecurityError, match="not allowed"):
            await executor.execute(
                "cat",
                ["./skills/../secret.txt"],
                cwd=workspace,
            )

    @pytest.mark.asyncio
    async def test_shell_embedded_traversal_is_validated(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "memory").mkdir()
        memory_disabled_executor = TerminalExecutor(allowed_path_prefixes=["./skills", "./code"])

        with pytest.raises(SecurityError, match="not allowed"):
            await memory_disabled_executor.execute(
                "bash",
                ["-c", "cat ../memory/private.txt"],
                cwd=workspace,
            )

    @pytest.mark.asyncio
    async def test_temp_roots_are_not_implicitly_allowed(self, tmp_path):
        """SECURITY: /tmp is a real palace location, never a blanket exemption."""
        secret = tmp_path / "memory" / "private.txt"
        secret.parent.mkdir(parents=True)
        secret.write_text("PRIVATE-CONTENT", encoding="utf-8")
        locked_down = TerminalExecutor(allowed_path_prefixes=[])

        for temp_root in ("/tmp", "/var/folders", "/private/var/folders"):
            with pytest.raises(SecurityError, match="not allowed"):
                await locked_down.execute("cat", [f"{temp_root}/memory/private.txt"])

    @pytest.mark.asyncio
    async def test_temp_directory_is_allowed_when_explicitly_listed(self, tmp_path):
        """Callers that need a temp directory opt in by listing it."""
        secret = tmp_path / "notes.txt"
        secret.write_text("explicitly allowed", encoding="utf-8")
        executor = TerminalExecutor(allowed_path_prefixes=[str(tmp_path)])

        result = await executor.execute("cat", [str(secret)])

        assert result.exit_code == 0
        assert "explicitly allowed" in result.stdout

    @pytest.mark.asyncio
    async def test_subdirectory_of_allowed_path_succeeds(self, executor, test_data_dir):
        """SECURITY: Test that subdirectories of allowed paths are accessible."""
        # Create nested directory structure
        nested_dir = test_data_dir / "test-data" / "nested" / "deep" / "path"
        nested_dir.mkdir(parents=True, exist_ok=True)
        nested_file = nested_dir / "file.txt"
        nested_file.write_text("Deep nested file")

        result = await executor.execute("cat", [str(nested_file)], cwd=test_data_dir)

        assert result.exit_code == 0
        assert "Deep nested file" in result.stdout

    @pytest.mark.asyncio
    async def test_command_with_no_path_arguments_succeeds(self, executor):
        """SECURITY: Test that commands with no path arguments work."""
        # Commands with only flags/values (no paths) should work
        result = await executor.execute("python3", ["--version"])

        assert result.exit_code == 0
        assert "Python" in result.stdout or "Python" in result.stderr

    @pytest.mark.asyncio
    async def test_empty_arguments_succeeds(self, executor):
        """SECURITY: Test that commands with empty arguments work."""
        # ls with no arguments should work (doesn't access filesystem via args)
        result = await executor.execute("ls", [])

        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_git_allowed_and_version_runs(self, executor):
        """SECURITY: git is on the allowlist and runs (requires git in PATH)."""
        result = await executor.execute("git", ["--version"])
        assert result.exit_code == 0
        assert "git version" in result.stdout.lower()

    @pytest.mark.asyncio
    async def test_gh_allowed_and_version_runs(self, executor):
        """SECURITY: gh is on the allowlist and runs (requires GitHub CLI in PATH)."""
        result = await executor.execute("gh", ["--version"])
        assert result.exit_code == 0
        assert "gh version" in result.stdout.lower()


class TestTerminalExecutorExecution:
    """
    Functional tests for Terminal Executor.
    
    These tests verify correct execution behavior for valid commands.
    """

    @pytest.mark.asyncio
    async def test_successful_command_returns_output(self, executor):
        """Test that successful command returns stdout."""
        result = await executor.execute("echo", ["Hello World"])

        assert result.exit_code == 0
        assert "Hello World" in result.stdout
        assert result.stderr == ""

    @pytest.mark.asyncio
    async def test_failed_command_returns_error(self, executor):
        """Test that failed command returns non-zero exit code."""
        # Python with invalid syntax should exit with error
        result = await executor.execute(
            "python3",
            ["-c", "import sys; sys.exit(1)"]
        )

        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_command_with_stderr(self, executor):
        """Test that command writing to stderr is captured."""
        result = await executor.execute(
            "python3",
            ["-c", "import sys; sys.stderr.write('Error message\\n')"]
        )

        assert "Error message" in result.stderr

    @pytest.mark.asyncio
    async def test_command_with_multiple_arguments(self, executor):
        """Test that commands with multiple arguments work."""
        result = await executor.execute(
            "python3",
            ["-c", "import sys; print('arg1'); print('arg2')"]
        )

        assert result.exit_code == 0
        assert "arg1" in result.stdout
        assert "arg2" in result.stdout

    @pytest.mark.asyncio
    async def test_execute_uses_workspace_cwd(self, executor, tmp_path, monkeypatch):
        """Commands resolve workspace-relative paths when cwd differs from workspace root."""
        agent_dir = tmp_path / "agent"
        workspace = agent_dir / "workspace"
        target = workspace / "skills" / "probe.txt"
        target.parent.mkdir(parents=True)
        target.write_text("workspace-hit", encoding="utf-8")
        monkeypatch.chdir(agent_dir)

        result = await executor.execute(
            "cat",
            ["./skills/probe.txt"],
            cwd=workspace,
        )

        assert result.exit_code == 0
        assert "workspace-hit" in result.stdout

    @pytest.mark.asyncio
    async def test_execute_absolutizes_google_application_credentials(
        self, executor, tmp_path, monkeypatch
    ):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        agent_dir = tmp_path / "agent"
        workspace = agent_dir / "workspace"
        workspace.mkdir(parents=True)
        creds = agent_dir / "gcp.json"
        creds.write_text("{}", encoding="utf-8")
        monkeypatch.chdir(agent_dir)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "gcp.json")

        seen_env: dict[str, str] = {}

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            del kwargs
            nonlocal seen_env
            seen_env = dict(env or {})
            proc = MagicMock()
            proc.pid = 12345
            proc.returncode = 0
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.stdout.read = AsyncMock(return_value=b"")
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await executor.execute("echo", ["hi"], cwd=workspace)
        assert seen_env["GOOGLE_APPLICATION_CREDENTIALS"] == str(creds.resolve())

    @pytest.mark.asyncio
    async def test_execute_with_cwd_inherits_path_from_host_env(
        self, executor, tmp_path, monkeypatch
    ):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("PATH", "/custom/bin:/usr/bin")

        seen_env: dict[str, str] = {}

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            del kwargs
            nonlocal seen_env
            seen_env = dict(env or {})
            proc = MagicMock()
            proc.pid = 12345
            proc.returncode = 0
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.stdout.read = AsyncMock(return_value=b"")
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await executor.execute("echo", ["hi"], cwd=workspace)
        import os
        import sys
        from pathlib import Path

        venv_bin = str(Path(sys.executable).resolve().parent)
        assert seen_env.get("PATH") == f"{venv_bin}{os.pathsep}/custom/bin:/usr/bin"
        assert seen_env.get("MONKEYBOT_PYTHON") == sys.executable

    @pytest.mark.asyncio
    async def test_generic_child_does_not_inherit_memory_routing(
        self, executor, tmp_path, monkeypatch
    ):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", "/private/palace-b")
        monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")
        monkeypatch.setenv("MEMORY_STORAGE_URI", "local:///private/palace-b")
        seen_env: dict[str, str] = {}

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            del cmd, stdout, stderr, cwd, kwargs
            nonlocal seen_env
            seen_env = dict(env or {})
            proc = MagicMock()
            proc.pid = 12345
            proc.returncode = 0
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.stdout.read = AsyncMock(return_value=b"")
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await executor.execute("bash", ["-c", "echo safe"], cwd=tmp_path)

        assert "MEMPALACE_PALACE_PATH" not in seen_env
        assert "MEMPALACE_BACKEND" not in seen_env
        assert "MEMORY_STORAGE_URI" not in seen_env

    @pytest.mark.asyncio
    async def test_direct_mempalace_uses_explicit_memory_route(
        self, executor, tmp_path, monkeypatch
    ):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", "/private/palace-b")
        seen_env: dict[str, str] = {}

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            del cmd, stdout, stderr, cwd, kwargs
            nonlocal seen_env
            seen_env = dict(env or {})
            proc = MagicMock()
            proc.pid = 12345
            proc.returncode = 0
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.stdout.read = AsyncMock(return_value=b"")
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await executor.execute(
            "mempalace",
            ["search", "query"],
            cwd=tmp_path,
            env_overrides={
                "MEMPALACE_PALACE_PATH": "/private/palace-a",
                "MEMPALACE_BACKEND": "chroma",
            },
        )

        assert seen_env["MEMPALACE_PALACE_PATH"] == "/private/palace-a"
        assert seen_env["MEMPALACE_BACKEND"] == "chroma"

    @pytest.mark.asyncio
    async def test_mempalace_uses_console_script_when_on_path(
        self, executor, tmp_path, monkeypatch
    ):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        seen: list[tuple[str, ...]] = []

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            del stdout, stderr, cwd, env, kwargs
            seen.append(cmd)
            proc = MagicMock()
            proc.pid = 1
            proc.returncode = 0
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.stdout.read = AsyncMock(return_value=b"")
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(
            "monkeybot.core.tools.terminal.shutil.which",
            lambda name, path=None: "/opt/bin/mempalace" if name == "mempalace" else None,
        )
        await executor.execute("mempalace", ["search", "q"], cwd=tmp_path)
        assert seen[0][0] == "/opt/bin/mempalace"
        assert seen[0][1:] == ("search", "q")

    @pytest.mark.asyncio
    async def test_mempalace_falls_back_to_python_module(
        self, executor, tmp_path, monkeypatch
    ):
        import asyncio
        import sys
        from unittest.mock import AsyncMock, MagicMock

        seen: list[tuple[str, ...]] = []

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            del stdout, stderr, cwd, env, kwargs
            seen.append(cmd)
            proc = MagicMock()
            proc.pid = 1
            proc.returncode = 0
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            proc.stdout.read = AsyncMock(return_value=b"")
            proc.stderr.read = AsyncMock(return_value=b"")
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr("monkeybot.core.tools.terminal.shutil.which", lambda *a, **k: None)
        await executor.execute("mempalace", ["search", "q"], cwd=tmp_path)
        assert seen[0][0] == sys.executable
        assert seen[0][1:3] == ("-m", "mempalace")
        assert seen[0][3:] == ("search", "q")

    @pytest.mark.asyncio
    async def test_mempalace_rejects_non_search_subcommand(self, executor, tmp_path):
        with pytest.raises(SecurityError, match="only 'search' is permitted"):
            await executor.execute("mempalace", ["repair", "rebuild-index"], cwd=tmp_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args",
        [
            ["--pre", "mempalace", ".", "README.md"],
            ["--pre=mempalace", ".", "README.md"],
            ["--hostname-bin=mempalace", "private", "README.md"],
        ],
    )
    async def test_ripgrep_rejects_process_launch_options(self, executor, tmp_path, args):
        with pytest.raises(SecurityError, match="launches a process"):
            await executor.execute("rg", args, cwd=tmp_path)


class TestTerminalExecutorTimeout:
    """
    Timeout handling tests for Terminal Executor.
    
    These tests verify that long-running processes are properly killed.
    """

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, executor):
        """Test that timeout kills long-running process."""
        from monkeybot.core.tools.terminal import CommandTimeoutError

        with pytest.raises(CommandTimeoutError) as exc_info:
            await executor.execute(
                "python3",
                ["-c", "import time; time.sleep(60)"],
                timeout=1
            )

        assert "timeout" in str(exc_info.value).lower()
        assert exc_info.value.timeout == 1

    @pytest.mark.asyncio
    async def test_timeout_preserves_partial_stdout(self, executor):
        """Drained stdout/stderr from before kill are attached to the error."""
        from monkeybot.core.tools.terminal import CommandTimeoutError

        with pytest.raises(CommandTimeoutError) as exc_info:
            await executor.execute(
                "python3",
                [
                    "-c",
                    "import sys, time; print('partial-out', flush=True); "
                    "print('partial-err', file=sys.stderr, flush=True); time.sleep(60)",
                ],
                timeout=1,
            )

        assert "partial-out" in exc_info.value.stdout
        assert "partial-err" in exc_info.value.stderr

    @pytest.mark.asyncio
    async def test_execute_does_not_hang_when_grandchild_holds_pipe(self, executor):
        """Child exit with a pipe-holding descendant must error, not report success."""
        from monkeybot.core.tools.terminal import CommandTimeoutError

        # Parent prints and exits; forked child keeps the PIPE open for 60s.
        # Unbounded stdout.read() would hang until the child exits.
        with pytest.raises(CommandTimeoutError) as exc_info:
            await asyncio.wait_for(
                executor.execute(
                    "python3",
                    [
                        "-c",
                        "import os, sys, time\n"
                        "if os.fork() == 0:\n"
                        "    time.sleep(60)\n"
                        "    os._exit(0)\n"
                        "print('done', flush=True)\n",
                    ],
                    timeout=5,
                ),
                timeout=8,
            )
        assert "descendant" in str(exc_info.value).lower() or "streams did not close" in str(
            exc_info.value
        ).lower()
        assert "done" in exc_info.value.stdout

    @pytest.mark.asyncio
    async def test_fast_command_does_not_timeout(self, executor):
        """Test that fast commands complete before timeout."""
        # Set generous timeout for fast command
        result = await executor.execute(
            "echo",
            ["Fast command"],
            timeout=5
        )

        assert result.exit_code == 0
        assert "Fast command" in result.stdout

    @pytest.mark.asyncio
    async def test_custom_timeout_value(self, executor):
        """Test that custom timeout values are respected."""
        # Command that sleeps 2 seconds should succeed with 3s timeout
        result = await executor.execute(
            "python3",
            ["-c", "import time; time.sleep(2); print('Done')"],
            timeout=3
        )

        assert result.exit_code == 0
        assert "Done" in result.stdout


class TestTerminalExecutorOutputTruncation:
    """
    Output truncation tests for Terminal Executor.
    
    These tests verify that large outputs don't cause memory issues.
    """

    @pytest.mark.asyncio
    async def test_large_stdout_truncated(self, executor):
        """Test that large stdout is truncated to 1MB."""
        # Generate 2MB of output
        result = await executor.execute(
            "python3",
            ["-c", "print('x' * (2 * 1024 * 1024))"]
        )

        # Should be truncated to ~1MB
        assert len(result.stdout) <= 1024 * 1024 + 200  # Small buffer for message
        assert "[Output truncated" in result.stdout

    @pytest.mark.asyncio
    async def test_large_stderr_truncated(self, executor):
        """Test that large stderr is truncated to 1MB."""
        # Generate 2MB of error output
        result = await executor.execute(
            "python3",
            ["-c", "import sys; sys.stderr.write('x' * (2 * 1024 * 1024))"]
        )

        # Should be truncated to ~1MB
        assert len(result.stderr) <= 1024 * 1024 + 200  # Small buffer for message
        assert "[Output truncated" in result.stderr

    @pytest.mark.asyncio
    async def test_small_output_not_truncated(self, executor):
        """Test that small outputs are not truncated."""
        result = await executor.execute(
            "echo",
            ["Small output"]
        )

        assert result.exit_code == 0
        assert "Small output" in result.stdout
        assert "[Output truncated" not in result.stdout


class TestTerminalExecutorEdgeCases:
    """
    Edge case tests for Terminal Executor.
    
    These tests verify handling of unusual but valid inputs.
    """

    @pytest.mark.asyncio
    async def test_command_with_special_characters_in_output(self, executor):
        """Test that special characters in output are handled correctly."""
        result = await executor.execute(
            "echo",
            ["Special chars: !@#$%^&*()[]{}"]
        )

        assert result.exit_code == 0
        assert "Special chars" in result.stdout

    @pytest.mark.asyncio
    async def test_command_with_unicode_output(self, executor):
        """Test that unicode output is decoded correctly."""
        result = await executor.execute(
            "python3",
            ["-c", "print('Unicode: 你好世界 🚀')"]
        )

        assert result.exit_code == 0
        # Unicode should be in output (or replaced if not valid UTF-8)
        assert "Unicode" in result.stdout

    @pytest.mark.asyncio
    async def test_command_with_empty_output(self, executor):
        """Test that commands with no output work correctly."""
        result = await executor.execute(
            "python3",
            ["-c", "pass"]
        )

        assert result.exit_code == 0
        assert result.stdout == ""
        assert result.stderr == ""

    @pytest.mark.asyncio
    async def test_execution_result_dataclass(self, executor):
        """Test that ExecutionResult is a proper dataclass."""
        result = await executor.execute("echo", ["test"])

        # Test dataclass properties
        assert hasattr(result, "stdout")
        assert hasattr(result, "stderr")
        assert hasattr(result, "exit_code")
        assert isinstance(result.stdout, str)
        assert isinstance(result.stderr, str)
        assert isinstance(result.exit_code, int)


class TestTerminalExecutorConstants:
    """
    Tests for security constants configuration.
    
    These tests document and verify the security configuration.
    """

    def test_allowed_commands_list(self):
        """Test that ALLOWED_COMMANDS contains expected commands."""
        # Document expected commands
        expected_commands = [
            "cat",
            "ls",
            "grep",
            "rg",
            "echo",
            "python",
            "python3",
            "uv",
            "git",
            "gh",
            "bash",
            "mempalace",
        ]

        for cmd in expected_commands:
            assert cmd in ALLOWED_COMMANDS, f"Expected command '{cmd}' not in ALLOWED_COMMANDS"

    def test_allowed_paths_list(self):
        """Test that ALLOWED_PATHS contains expected paths."""
        # Document expected paths
        expected_paths = [
            "../memory/",
            "../memory",
            "./skills/",
            "./skills",
            "./global-skills/",
            "./global-skills",
            "./test-data/",
            "./code/",
            "./code",
        ]

        for path in expected_paths:
            assert path in ALLOWED_PATHS, f"Expected path '{path}' not in ALLOWED_PATHS"

    def test_no_dangerous_commands_in_allowlist(self):
        """Test that dangerous commands are not in allowlist."""
        dangerous_commands = [
            "rm", "rmdir", "del",  # Deletion
            "curl", "wget",         # Network access
            "sudo", "su",           # Privilege escalation
            "chmod", "chown",       # Permission changes
            "kill", "killall",      # Process manipulation
            "eval", "exec",         # Code execution
        ]

        for cmd in dangerous_commands:
            assert cmd not in ALLOWED_COMMANDS, f"Dangerous command '{cmd}' found in ALLOWED_COMMANDS!"


@pytest.mark.skipif(
    not isolation_support().available,
    reason=f"host cannot isolate filesystems: {isolation_support().detail}",
)
class TestTerminalExecutorFilesystemIsolation:
    """A hidden directory must be unreachable however the child spells the path."""

    @pytest.fixture
    def hidden_secret(self, tmp_path):
        secret_dir = tmp_path / "memory"
        secret_dir.mkdir()
        (secret_dir / "private.txt").write_text("PRIVATE-CONTENT", encoding="utf-8")
        return secret_dir

    @pytest.fixture
    def isolated_executor(self, tmp_path, hidden_secret):
        return TerminalExecutor(
            allowed_path_prefixes=[str(tmp_path)],
            hidden_paths=[hidden_secret],
        )

    @pytest.mark.asyncio
    async def test_direct_read_of_hidden_path_finds_nothing(self, isolated_executor, hidden_secret):
        result = await isolated_executor.execute("cat", [str(hidden_secret / "private.txt")])

        assert result.exit_code != 0
        assert "PRIVATE-CONTENT" not in result.stdout

    @pytest.mark.asyncio
    async def test_shell_variable_indirection_cannot_reach_hidden_path(
        self, isolated_executor, hidden_secret
    ):
        result = await isolated_executor.execute(
            "bash", ["-c", f'p={hidden_secret}; cat "$p"/private.txt']
        )

        assert "PRIVATE-CONTENT" not in result.stdout

    @pytest.mark.asyncio
    async def test_interpreter_file_io_cannot_reach_hidden_path(
        self, isolated_executor, hidden_secret
    ):
        result = await isolated_executor.execute(
            "python",
            ["-c", f"print(open({str(hidden_secret / 'private.txt')!r}).read())"],
        )

        assert result.exit_code != 0
        assert "PRIVATE-CONTENT" not in result.stdout

    @pytest.mark.asyncio
    async def test_hidden_directory_appears_empty(self, isolated_executor, hidden_secret):
        result = await isolated_executor.execute("bash", ["-c", f"ls -A {hidden_secret}"])

        assert result.stdout.strip() == ""

    @pytest.mark.asyncio
    async def test_visible_paths_and_tooling_still_work(self, isolated_executor, tmp_path):
        visible = tmp_path / "notes.txt"
        visible.write_text("VISIBLE", encoding="utf-8")

        read = await isolated_executor.execute("cat", [str(visible)])
        shell = await isolated_executor.execute("bash", ["-c", "echo shell-ok"])

        assert read.exit_code == 0 and "VISIBLE" in read.stdout
        assert shell.exit_code == 0 and "shell-ok" in shell.stdout

    @pytest.mark.asyncio
    async def test_host_filesystem_is_unchanged(self, isolated_executor, hidden_secret):
        await isolated_executor.execute("bash", ["-c", f"ls -A {hidden_secret}"])

        assert (hidden_secret / "private.txt").read_text(encoding="utf-8") == "PRIVATE-CONTENT"

    @pytest.mark.asyncio
    async def test_directory_created_after_construction_is_still_hidden(self, tmp_path):
        late = tmp_path / "late-memory"
        executor = TerminalExecutor(allowed_path_prefixes=[str(tmp_path)], hidden_paths=[late])
        late.mkdir()
        (late / "private.txt").write_text("PRIVATE-CONTENT", encoding="utf-8")

        result = await executor.execute("bash", ["-c", f"cat {late}/private.txt"])

        assert "PRIVATE-CONTENT" not in result.stdout


class TestTerminalExecutorIsolationFallback:
    """Isolation is never silently assumed."""

    @pytest.mark.asyncio
    async def test_unavailable_isolation_refuses_to_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            terminal_module,
            "isolation_support",
            lambda: IsolationSupport("none", "probe says no"),
        )
        executor = TerminalExecutor(
            allowed_path_prefixes=[str(tmp_path)], hidden_paths=[tmp_path / "memory"]
        )

        with pytest.raises(SecurityError, match="Filesystem isolation is unavailable"):
            await executor.execute("echo", ["should-not-run"])

    @pytest.mark.asyncio
    async def test_failed_isolation_setup_raises_security_error(self, tmp_path, monkeypatch):
        def broken_argv(executable, args, hidden_paths, *, support):
            del executable, args, hidden_paths, support
            return "bash", [
                "-c",
                f"echo '{ISOLATION_ERROR_PREFIX} unshare failed' >&2; "
                f"exit {ISOLATION_FAILURE_EXIT_CODE}",
            ]

        monkeypatch.setattr(
            terminal_module,
            "isolation_support",
            lambda: IsolationSupport("namespace", "forced"),
        )
        monkeypatch.setattr(terminal_module, "isolated_argv", broken_argv)
        executor = TerminalExecutor(
            allowed_path_prefixes=[str(tmp_path)], hidden_paths=[tmp_path / "memory"]
        )

        with pytest.raises(SecurityError, match="isolation could not be established"):
            await executor.execute("echo", ["should-not-run"])


class TestTerminalExecutorRestricted:
    """`restricted()` narrows policy without mutating the caller's executor."""

    def test_restricted_copy_leaves_original_untouched(self, tmp_path):
        original = TerminalExecutor(
            allowed_commands=["cat", "mempalace"], allowed_path_prefixes=["../memory", "./skills"]
        )

        derived = original.restricted(
            allowed_commands=["cat"],
            allowed_path_prefixes=["./skills"],
            hidden_paths=[tmp_path / "memory"],
        )

        assert original.allowed_commands == ("cat", "mempalace")
        assert original.allowed_path_prefixes == ("../memory", "./skills")
        assert original.hidden_paths == ()
        assert derived.allowed_commands == ("cat",)
        assert derived.allowed_path_prefixes == ("./skills",)
        assert derived.hidden_paths == (tmp_path / "memory",)

    def test_restricted_defaults_inherit_from_source(self):
        original = TerminalExecutor(allowed_commands=["cat"], allowed_path_prefixes=["./skills"])

        derived = original.restricted()

        assert derived.allowed_commands == original.allowed_commands
        assert derived.allowed_path_prefixes == original.allowed_path_prefixes

    def test_restricted_none_preserves_hidden_paths(self, tmp_path):
        original = TerminalExecutor(hidden_paths=[tmp_path / "extra"])

        derived = original.restricted(hidden_paths=None)

        assert derived.hidden_paths == original.hidden_paths
        assert derived.hidden_paths == (tmp_path / "extra",)


def test_build_skill_runtime_env_prepends_gateway_python_bin(tmp_path, monkeypatch) -> None:
    """bash -c python3 must see the gateway venv before system python."""
    import os
    import sys
    from pathlib import Path

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = build_skill_runtime_env(cwd=tmp_path)
    venv_bin = str(Path(sys.executable).resolve().parent)
    assert env["PATH"].split(os.pathsep)[0] == venv_bin
    assert env["MONKEYBOT_PYTHON"] == sys.executable
    assert env["MONKEYBOT_WORKSPACE_ROOT"] == str(tmp_path.resolve())


def test_process_group_helpers_skip_on_windows(monkeypatch) -> None:
    import monkeybot.core.tools.terminal as terminal

    monkeypatch.setattr(terminal, "_SUPPORTS_PROCESS_GROUPS", False)
    assert terminal._process_group_id(12345) is None

    class _Proc:
        returncode = None
        killed = False

        def kill(self) -> None:
            self.killed = True

    proc = _Proc()
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        terminal.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
    )
    terminal._kill_process_group(999, proc)  # type: ignore[arg-type]
    assert calls == []
    assert proc.killed is True
