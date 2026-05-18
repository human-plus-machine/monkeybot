"""Tests for SandboxConfig and SandboxExecutor.

All tests mock opensandbox.Sandbox — the SDK does not need to be installed to
run this suite. Tests are structured to catch real failure points:

- Security: binary allowlist must fire BEFORE sandbox creation
- Config: bad env values must raise, not silently produce wrong state
- Lifecycle: sandbox is lazy-created and reused; aclose() is idempotent
- Result mapping: non-zero exit does not raise; None exit_code defaults to 0
- Cleanup: kill() errors are swallowed so gateway finally blocks always complete
- Workspace: absolute path is passed to the container mount
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monkeybot.core.tools.sandbox_executor import SandboxConfig, SandboxExecutor
from monkeybot.core.tools.terminal import ExecutionResult, SecurityError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execution(exit_code=0, stdout_text="", stderr_text=""):
    """Build a mock opensandbox execution result."""
    stdout_entry = MagicMock()
    stdout_entry.text = stdout_text
    stderr_entry = MagicMock()
    stderr_entry.text = stderr_text

    execution = MagicMock()
    execution.exit_code = exit_code
    execution.logs.stdout = [stdout_entry] if stdout_text else []
    execution.logs.stderr = [stderr_entry] if stderr_text else []
    return execution


def _mock_sandbox(execution=None):
    """Return a mock Sandbox instance whose commands.run returns execution."""
    sandbox = MagicMock()
    sandbox.id = "test-sandbox-id"
    sandbox.commands.run = AsyncMock(return_value=execution or _make_execution())
    sandbox.kill = AsyncMock()
    return sandbox


def _make_create_mock(sandbox=None):
    """Return a mock Sandbox class whose create() returns the given sandbox."""
    sb = sandbox or _mock_sandbox()
    mock_cls = MagicMock()
    mock_cls.create = AsyncMock(return_value=sb)
    return mock_cls, sb


def _make_opensandbox_module(mock_cls):
    """Build a mock opensandbox module with all symbols _ensure_sandbox imports."""
    mod = MagicMock()
    mod.Sandbox = mock_cls

    # ConnectionConfig: capture kwargs so tests can inspect domain/api_key/protocol
    mod.config = MagicMock()
    mod.config.ConnectionConfig = MagicMock(side_effect=lambda **kw: MagicMock(**kw))

    # Volume / Host: lightweight stand-ins that store their init kwargs
    class _Volume:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Host:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mod.models = MagicMock()
    mod.models.sandboxes = MagicMock()
    mod.models.sandboxes.Volume = _Volume
    mod.models.sandboxes.Host = _Host

    # execute() imports RunCommandOpts from opensandbox.models.execd
    execd_mod = ModuleType("opensandbox.models.execd")

    class _RunCommandOpts:
        def __init__(
            self,
            *,
            timeout=None,
            background=False,
            working_directory=None,
            uid=None,
            gid=None,
            envs=None,
        ):
            self.timeout = timeout
            self.background = background
            self.working_directory = working_directory
            self.uid = uid
            self.gid = gid
            self.envs = envs

    execd_mod.RunCommandOpts = _RunCommandOpts
    mod.models.execd = execd_mod
    return mod


def _opensandbox_sys_modules(osb):
    return {
        "opensandbox": osb,
        "opensandbox.config": osb.config,
        "opensandbox.models.sandboxes": osb.models.sandboxes,
        "opensandbox.models.execd": osb.models.execd,
    }


# ---------------------------------------------------------------------------
# SandboxConfig.from_env()
# ---------------------------------------------------------------------------

class TestSandboxConfigFromEnv:
    def test_defaults_when_no_env_vars_set(self, monkeypatch):
        for key in ("SANDBOX_ENABLED", "SANDBOX_SERVER_URL", "SANDBOX_IMAGE",
                    "SANDBOX_API_KEY", "SANDBOX_TTL_SECONDS", "SANDBOX_USE_SERVER_PROXY"):
            monkeypatch.delenv(key, raising=False)

        cfg = SandboxConfig.from_env()

        assert cfg.enabled is False
        assert cfg.server_url == "http://localhost:8080"
        assert cfg.image == "python:3.12"
        assert cfg.api_key is None
        assert cfg.ttl_seconds == 1800
        assert cfg.use_server_proxy is True

    def test_use_server_proxy_false_when_env_disabled(self, monkeypatch):
        for key in ("SANDBOX_ENABLED", "SANDBOX_SERVER_URL", "SANDBOX_IMAGE",
                    "SANDBOX_API_KEY", "SANDBOX_TTL_SECONDS"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SANDBOX_USE_SERVER_PROXY", "false")
        assert SandboxConfig.from_env().use_server_proxy is False

    def test_enabled_true_lowercase(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        assert SandboxConfig.from_env().enabled is True

    def test_enabled_true_uppercase(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "TRUE")
        assert SandboxConfig.from_env().enabled is True

    def test_enabled_false(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "false")
        assert SandboxConfig.from_env().enabled is False

    def test_enabled_1_is_not_true(self, monkeypatch):
        # Only the exact string "true" (case-insensitive) enables sandbox.
        # "1" is truthy in Python but must NOT enable it to avoid accidental activation.
        monkeypatch.setenv("SANDBOX_ENABLED", "1")
        assert SandboxConfig.from_env().enabled is False

    def test_enabled_yes_is_not_true(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "yes")
        assert SandboxConfig.from_env().enabled is False

    def test_custom_server_url(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_SERVER_URL", "http://myserver:9090")
        assert SandboxConfig.from_env().server_url == "http://myserver:9090"

    def test_custom_image(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_IMAGE", "python:3.11")
        assert SandboxConfig.from_env().image == "python:3.11"

    def test_api_key_set(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_API_KEY", "secret-key")
        assert SandboxConfig.from_env().api_key == "secret-key"

    def test_api_key_empty_string_becomes_none(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_API_KEY", "")
        assert SandboxConfig.from_env().api_key is None

    def test_custom_ttl(self, monkeypatch):
        monkeypatch.setenv("SANDBOX_TTL_SECONDS", "600")
        assert SandboxConfig.from_env().ttl_seconds == 600

    def test_invalid_ttl_raises_value_error(self, monkeypatch):
        # Bad config must fail loudly at startup, not silently default to 0.
        monkeypatch.setenv("SANDBOX_TTL_SECONDS", "notanint")
        with pytest.raises(ValueError, match="SANDBOX_TTL_SECONDS"):
            SandboxConfig.from_env()


# ---------------------------------------------------------------------------
# SandboxExecutor — security: allowlist before sandbox creation
# ---------------------------------------------------------------------------

class TestSandboxExecutorAllowlist:
    def _executor(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        return SandboxExecutor(cfg, tmp_path)

    @pytest.mark.asyncio
    async def test_blocked_command_raises_security_error_before_sandbox_create(
        self, tmp_path
    ):
        executor = self._executor(tmp_path)
        mock_cls, _ = _make_create_mock()
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            with pytest.raises(SecurityError, match="not allowed"):
                await executor.execute("rm", ["-rf", "/"])

        # Sandbox.create must never have been called
        mock_cls.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_command_passes_allowlist(self, tmp_path):
        executor = self._executor(tmp_path)
        mock_cls, sandbox = _make_create_mock(_mock_sandbox(_make_execution(stdout_text="hi")))
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            result = await executor.execute("echo", ["hi"])

        assert result.stdout == "hi"
        mock_cls.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_curl_is_blocked(self, tmp_path):
        executor = self._executor(tmp_path)
        mock_cls, _ = _make_create_mock()
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            with pytest.raises(SecurityError):
                await executor.execute("curl", ["https://example.com"])

        mock_cls.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_wget_is_blocked(self, tmp_path):
        executor = self._executor(tmp_path)
        mock_cls, _ = _make_create_mock()
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            with pytest.raises(SecurityError):
                await executor.execute("wget", ["https://example.com"])

        mock_cls.create.assert_not_called()


# ---------------------------------------------------------------------------
# SandboxExecutor — lazy creation and sandbox reuse
# ---------------------------------------------------------------------------

class TestSandboxExecutorLazyCreation:
    def _executor(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        return SandboxExecutor(cfg, tmp_path)

    @pytest.mark.asyncio
    async def test_sandbox_created_on_first_execute(self, tmp_path):
        executor = self._executor(tmp_path)
        mock_cls, _ = _make_create_mock()
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            await executor.execute("echo", ["hello"])

        mock_cls.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_sandbox_reused_on_second_execute(self, tmp_path):
        executor = self._executor(tmp_path)
        mock_cls, _ = _make_create_mock()
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            await executor.execute("echo", ["first"])
            await executor.execute("echo", ["second"])

        # Only one container created across both calls
        mock_cls.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_before_execute_is_noop(self, tmp_path):
        executor = self._executor(tmp_path)
        mock_cls, _ = _make_create_mock()
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            await executor.aclose()  # no sandbox yet — must not raise

        mock_cls.create.assert_not_called()


# ---------------------------------------------------------------------------
# SandboxExecutor — SDK not installed
# ---------------------------------------------------------------------------

class TestSandboxExecutorSdkNotInstalled:
    @pytest.mark.asyncio
    async def test_missing_sdk_raises_runtime_error_with_install_hint(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)

        # Simulate opensandbox not being installed (all sub-modules also absent)
        with patch.dict(sys.modules, {
            "opensandbox": None,
            "opensandbox.config": None,
            "opensandbox.models.sandboxes": None,
            "opensandbox.models.execd": None,
        }), pytest.raises(RuntimeError) as exc_info:
            await executor.execute("echo", ["hello"])

        msg = str(exc_info.value)
        assert "pip install" in msg
        assert "[sandbox]" in msg


# ---------------------------------------------------------------------------
# SandboxExecutor — execution result mapping
# ---------------------------------------------------------------------------

class TestSandboxExecutorResultMapping:
    def _executor(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        return SandboxExecutor(cfg, tmp_path)

    @pytest.mark.asyncio
    async def test_success_result_mapped_correctly(self, tmp_path):
        executor = self._executor(tmp_path)
        execution = _make_execution(exit_code=0, stdout_text="hello", stderr_text="")
        mock_cls, _ = _make_create_mock(_mock_sandbox(execution))
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            result = await executor.execute("echo", ["hello"])

        assert isinstance(result, ExecutionResult)
        assert result.exit_code == 0
        assert result.stdout == "hello"
        assert result.stderr == ""

    @pytest.mark.asyncio
    async def test_nonzero_exit_does_not_raise(self, tmp_path):
        # A failed command must return a result, not raise an exception.
        # If this regresses, any failed shell command crashes the turn.
        executor = self._executor(tmp_path)
        execution = _make_execution(exit_code=1, stderr_text="oops")
        mock_cls, _ = _make_create_mock(_mock_sandbox(execution))
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            result = await executor.execute("python3", ["-c", "exit(1)"])

        assert result.exit_code == 1
        assert result.stderr == "oops"

    @pytest.mark.asyncio
    async def test_empty_stdout_list_does_not_crash(self, tmp_path):
        executor = self._executor(tmp_path)
        execution = _make_execution(exit_code=0)  # empty stdout/stderr lists
        mock_cls, _ = _make_create_mock(_mock_sandbox(execution))
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            result = await executor.execute("echo", [])

        assert result.stdout == ""
        assert result.stderr == ""

    @pytest.mark.asyncio
    async def test_none_exit_code_defaults_to_zero(self, tmp_path):
        # Defensive: same pattern as TerminalExecutor's `process.returncode or 0`.
        executor = self._executor(tmp_path)
        execution = _make_execution(exit_code=None)
        mock_cls, _ = _make_create_mock(_mock_sandbox(execution))
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            result = await executor.execute("echo", [])

        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_stderr_captured_alongside_stdout(self, tmp_path):
        executor = self._executor(tmp_path)
        execution = _make_execution(exit_code=0, stdout_text="out", stderr_text="err")
        mock_cls, _ = _make_create_mock(_mock_sandbox(execution))
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            result = await executor.execute("python3", ["-c", "pass"])

        assert result.stdout == "out"
        assert result.stderr == "err"


# ---------------------------------------------------------------------------
# SandboxExecutor — cleanup / aclose()
# ---------------------------------------------------------------------------

class TestSandboxExecutorCleanup:
    def _executor(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        return SandboxExecutor(cfg, tmp_path)

    @pytest.mark.asyncio
    async def test_aclose_calls_kill_and_clears_sandbox(self, tmp_path):
        executor = self._executor(tmp_path)
        sandbox = _mock_sandbox()
        mock_cls, _ = _make_create_mock(sandbox)
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            await executor.execute("echo", [])
            await executor.aclose()

        sandbox.kill.assert_called_once()
        assert executor._sandbox is None

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, tmp_path):
        # aclose() is called from error paths as well as normal paths.
        # Calling it twice must not double-kill or raise.
        executor = self._executor(tmp_path)
        sandbox = _mock_sandbox()
        mock_cls, _ = _make_create_mock(sandbox)
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            await executor.execute("echo", [])
            await executor.aclose()
            await executor.aclose()  # second call — must be a no-op

        sandbox.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_swallows_kill_exception(self, tmp_path):
        # If kill() raises, the exception must NOT propagate.
        # Gateway finally blocks must always complete cleanly.
        executor = self._executor(tmp_path)
        sandbox = _mock_sandbox()
        sandbox.kill = AsyncMock(side_effect=RuntimeError("container gone"))
        mock_cls, _ = _make_create_mock(sandbox)
        osb = _make_opensandbox_module(mock_cls)

        with patch.dict(sys.modules, _opensandbox_sys_modules(osb)):
            await executor.execute("echo", [])
            await executor.aclose()  # must not raise even though kill() failed

        assert executor._sandbox is None


# ---------------------------------------------------------------------------
# SandboxExecutor — workspace mount
# ---------------------------------------------------------------------------

class TestSandboxExecutorWorkspaceMount:
    def _patch_modules(self, mock_cls):
        osb = _make_opensandbox_module(mock_cls)
        return osb, _opensandbox_sys_modules(osb)

    @pytest.mark.asyncio
    async def test_absolute_workspace_path_in_volume(self, tmp_path):
        abs_path = str(tmp_path.resolve())
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            await executor.execute("echo", [])

        _, kwargs = mock_cls.create.call_args
        volumes = kwargs.get("volumes", [])
        assert len(volumes) == 1
        vol = volumes[0]
        assert vol.host.path == abs_path
        assert vol.mountPath == abs_path
        assert vol.readOnly is False

    @pytest.mark.asyncio
    async def test_relative_workspace_resolved_to_absolute(self, tmp_path, monkeypatch):
        # Relative paths passed to the constructor must be resolved before
        # being handed to Sandbox.create — a relative path in the container
        # would cause all file operations to fail silently.
        monkeypatch.chdir(tmp_path)
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, Path("."))
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            await executor.execute("echo", [])

        _, kwargs = mock_cls.create.call_args
        volumes = kwargs.get("volumes", [])
        assert Path(volumes[0].host.path).is_absolute()

    @pytest.mark.asyncio
    async def test_server_url_forwarded_as_connection_config_domain(self, tmp_path):
        # SANDBOX_SERVER_URL must reach the SDK via ConnectionConfig(domain=...).
        # This was previously broken — server_url was stored but never forwarded.
        cfg = SandboxConfig(
            enabled=True, server_url="http://custom-server:9999",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            await executor.execute("echo", [])

        # ConnectionConfig must have been constructed with the correct domain
        osb.config.ConnectionConfig.assert_called_once()
        _, cc_kwargs = osb.config.ConnectionConfig.call_args
        assert cc_kwargs.get("domain") == "custom-server:9999"
        assert cc_kwargs.get("protocol") == "http"
        assert cc_kwargs.get("use_server_proxy") is True

    @pytest.mark.asyncio
    async def test_https_server_url_forwarded_correctly(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="https://secure-server:443",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            await executor.execute("echo", [])

        _, cc_kwargs = osb.config.ConnectionConfig.call_args
        assert cc_kwargs.get("domain") == "secure-server:443"
        assert cc_kwargs.get("protocol") == "https"
        assert cc_kwargs.get("use_server_proxy") is True

    @pytest.mark.asyncio
    async def test_invalid_server_url_raises_value_error(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="not-a-url",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            with pytest.raises(ValueError, match="SANDBOX_SERVER_URL"):
                await executor.execute("echo", [])

    @pytest.mark.asyncio
    async def test_api_key_passed_via_connection_config(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key="mykey", image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            await executor.execute("echo", [])

        _, cc_kwargs = osb.config.ConnectionConfig.call_args
        assert cc_kwargs.get("api_key") == "mykey"
        assert cc_kwargs.get("use_server_proxy") is True

    @pytest.mark.asyncio
    async def test_api_key_none_when_not_set(self, tmp_path):
        cfg = SandboxConfig(
            enabled=True, server_url="http://localhost:8080",
            api_key=None, image="python:3.12", ttl_seconds=1800,
        )
        executor = SandboxExecutor(cfg, tmp_path)
        mock_cls, _ = _make_create_mock()
        osb, patches = self._patch_modules(mock_cls)

        with patch.dict(sys.modules, patches):
            await executor.execute("echo", [])

        _, cc_kwargs = osb.config.ConnectionConfig.call_args
        assert cc_kwargs.get("api_key") is None
        assert cc_kwargs.get("use_server_proxy") is True
