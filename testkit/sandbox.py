"""opensandbox mocks shared by sandbox and background-job tests."""

from __future__ import annotations

from types import ModuleType
from unittest.mock import AsyncMock, MagicMock


def _make_execution(
    exit_code: int = 0,
    stdout_text: str = "",
    stderr_text: str = "",
) -> MagicMock:
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


def _mock_sandbox(execution: MagicMock | None = None) -> MagicMock:
    """Return a mock Sandbox instance whose commands.run returns execution."""
    sandbox = MagicMock()
    sandbox.id = "test-sandbox-id"
    sandbox.commands.run = AsyncMock(return_value=execution or _make_execution())
    sandbox.kill = AsyncMock()
    return sandbox


def _make_create_mock(sandbox: MagicMock | None = None) -> tuple[MagicMock, MagicMock]:
    """Return a mock Sandbox class whose create() returns the given sandbox."""
    sb = sandbox or _mock_sandbox()
    mock_cls = MagicMock()
    mock_cls.create = AsyncMock(return_value=sb)
    return mock_cls, sb


def _make_opensandbox_module(mock_cls: MagicMock) -> MagicMock:
    """Build a mock opensandbox module with all symbols _ensure_sandbox imports."""
    mod = MagicMock()
    mod.Sandbox = mock_cls

    mod.config = MagicMock()
    mod.config.ConnectionConfig = MagicMock(side_effect=lambda **kw: MagicMock(**kw))

    class _Volume:
        def __init__(self, **kw: object) -> None:
            self.__dict__.update(kw)

    class _Host:
        def __init__(self, **kw: object) -> None:
            self.__dict__.update(kw)

    mod.models = MagicMock()
    mod.models.sandboxes = MagicMock()
    mod.models.sandboxes.Volume = _Volume
    mod.models.sandboxes.Host = _Host

    execd_mod = ModuleType("opensandbox.models.execd")

    class _RunCommandOpts:
        def __init__(
            self,
            *,
            timeout: object = None,
            background: bool = False,
            working_directory: object = None,
            uid: object = None,
            gid: object = None,
            envs: object = None,
        ) -> None:
            self.timeout = timeout
            self.background = background
            self.working_directory = working_directory
            self.uid = uid
            self.gid = gid
            self.envs = envs

    execd_mod.RunCommandOpts = _RunCommandOpts  # type: ignore[attr-defined]
    mod.models.execd = execd_mod
    return mod


def _opensandbox_sys_modules(osb: MagicMock) -> dict[str, object]:
    return {
        "opensandbox": osb,
        "opensandbox.config": osb.config,
        "opensandbox.models.sandboxes": osb.models.sandboxes,
        "opensandbox.models.execd": osb.models.execd,
    }
