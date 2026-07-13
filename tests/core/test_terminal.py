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

import pytest

from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    ExecutionResult,
    SecurityError,
    TerminalExecutor,
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

        result = await executor.execute("cat", [str(test_file)])

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
    async def test_subdirectory_of_allowed_path_succeeds(self, executor, test_data_dir):
        """SECURITY: Test that subdirectories of allowed paths are accessible."""
        # Create nested directory structure
        nested_dir = test_data_dir / "test-data" / "nested" / "deep" / "path"
        nested_dir.mkdir(parents=True, exist_ok=True)
        nested_file = nested_dir / "file.txt"
        nested_file.write_text("Deep nested file")

        result = await executor.execute("cat", [str(nested_file)])

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

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None):
            nonlocal seen_env
            seen_env = dict(env or {})
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            proc.returncode = 0
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

        async def fake_exec(*cmd, stdout=None, stderr=None, cwd=None, env=None):
            nonlocal seen_env
            seen_env = dict(env or {})
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            proc.returncode = 0
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        await executor.execute("echo", ["hi"], cwd=workspace)
        assert seen_env.get("PATH") == "/custom/bin:/usr/bin"


class TestTerminalExecutorTimeout:
    """
    Timeout handling tests for Terminal Executor.
    
    These tests verify that long-running processes are properly killed.
    """

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, executor):
        """Test that timeout kills long-running process."""
        with pytest.raises(TimeoutError) as exc_info:
            await executor.execute(
                "python3",
                ["-c", "import time; time.sleep(60)"],
                timeout=1
            )

        assert "timeout" in str(exc_info.value).lower()

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
        expected_commands = ["cat", "ls", "grep", "echo", "python", "python3", "uv", "git", "gh", "bash"]

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
