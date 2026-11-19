"""Spawn and teardown for chat, talk, and the gateway manager.

The port-release spike in ``spikes/chat_gateway_orphan_port.py`` is the
process-level regression. These tests lock the wiring: new sessions, group
SIGTERM, and escalation when a shim exits before its grandchild.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import monkeybot_cli.process_tree as process_tree
from monkeybot_cli.commands.chat import _spawn_gateway, run_chat
from monkeybot_cli.realtime import gateway_manager, talk_ui
from monkeybot_cli.runtime_python import RuntimePython


class _Proc:
    def __init__(self) -> None:
        self.pid = 50
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def send_signal(self, sig: int) -> None:
        del sig


def test_stop_process_group_returns_when_the_group_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", False)
    proc = _Proc()
    signals: list[int] = []
    killed: list[int] = []
    monkeypatch.setattr(process_tree, "signal_process_group", lambda p, sig: signals.append(sig))
    monkeypatch.setattr(process_tree, "process_group_alive", lambda pid: False)
    monkeypatch.setattr(process_tree, "kill_process_tree", lambda pid: killed.append(pid))

    process_tree.stop_process_group(proc, shutdown_timeout=1, kill_timeout=0.1)

    assert signals == [signal.SIGTERM]
    assert killed == []
    assert proc.returncode == 0


def test_stop_process_group_kills_group_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shim can _exit on SIGTERM while the listener ignores it."""
    monkeypatch.setattr(process_tree, "IS_WINDOWS", False)
    proc = _Proc()
    alive = True
    killed: list[int] = []

    def group_alive(pid: int) -> bool:
        del pid
        return alive

    def kill(pid: int) -> None:
        nonlocal alive
        alive = False
        killed.append(pid)

    monkeypatch.setattr(process_tree, "signal_process_group", lambda p, sig: None)
    monkeypatch.setattr(process_tree, "process_group_alive", group_alive)
    monkeypatch.setattr(process_tree, "kill_process_tree", kill)

    process_tree.stop_process_group(proc, shutdown_timeout=0.25, kill_timeout=0.1)

    assert killed == [50]


def test_signal_process_group_uses_killpg_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", False)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_tree.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    process_tree.signal_process_group(_Proc(), signal.SIGTERM)
    assert calls == [(50, signal.SIGTERM)]


def test_signal_process_group_uses_send_signal_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", True)
    proc = _Proc()
    sent: list[int] = []
    proc.send_signal = sent.append  # type: ignore[method-assign]
    process_tree.signal_process_group(proc, signal.SIGTERM)
    assert sent == [signal.SIGTERM]


def test_spawn_gateway_starts_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_popen(*_args: object, **kwargs: object) -> object:
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        "monkeybot_cli.commands.chat.prepare_runtime_python",
        lambda *_a, **_k: RuntimePython(["python"], "cli"),
    )
    monkeypatch.setattr("monkeybot_cli.commands.chat.subprocess.Popen", fake_popen)
    spawned = _spawn_gateway(None, tmp_path, 8123)
    try:
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        for key, value in process_tree.popen_kwargs_for_platform().items():
            assert kwargs[key] == value
    finally:
        spawned.log_file.close()
        spawned.log_path.unlink()


def test_spawn_combined_gateway_starts_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_popen(*_args: object, **kwargs: object) -> object:
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        talk_ui,
        "prepare_runtime_python",
        lambda *_a, **_k: RuntimePython(["python"], "cli"),
    )
    monkeypatch.setattr(talk_ui.subprocess, "Popen", fake_popen)
    spawned = talk_ui._spawn_combined_gateway(None, tmp_path, 8123)
    try:
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        for key, value in process_tree.popen_kwargs_for_platform().items():
            assert kwargs[key] == value
    finally:
        spawned.log_file.close()
        spawned.log_path.unlink()


def _chat_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        cwd=str(tmp_path),
        config=None,
        attach=False,
        url=None,
        port=None,
        model_provider=None,
        model_name=None,
        show_thinking=False,
        verbose=False,
        usage=False,
        session=None,
        resume_last=False,
        no_animations=True,
        theme="auto",
    )


def _agent(tmp_path: Path) -> None:
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "model:\n  provider: gemini\n  name: test\nruntime:\n  port: 18080\n",
        encoding="utf-8",
    )


def test_run_chat_session_exit_stops_the_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _agent(tmp_path)
    proc = MagicMock()
    proc.pid = 42
    proc.poll.return_value = None
    log_path = tmp_path / "gateway.log"
    log_file = log_path.open("w+", encoding="utf-8")
    from monkeybot_cli.commands.chat import _SpawnedGateway

    spawned = _SpawnedGateway(proc=proc, log_path=log_path, log_file=log_file)
    monkeypatch.setenv("MONKEYBOT_CHAT_PLAIN", "1")

    async def session(*_args: object, **_kwargs: object) -> int:
        return 0

    with (
        patch(
            "monkeybot_cli.commands.chat.resolve_config",
            return_value=tmp_path / "monkeybot_config" / "monkeybot.yaml",
        ),
        patch("monkeybot_cli.commands.chat.load_agent_dotenv", return_value=None),
        patch("monkeybot_cli.commands.chat._occupied_gateway_message", return_value=None),
        patch("monkeybot_cli.commands.chat.is_sandbox_enabled", return_value=False),
        patch("monkeybot_cli.commands.chat._spawn_gateway", return_value=spawned),
        patch("monkeybot_cli.commands.chat._wait_for_health", return_value=True),
        patch("monkeybot_cli.commands.chat._plain_chat_session", session),
        patch("monkeybot_cli.commands.chat.stop_process_group") as stop,
    ):
        code = run_chat(_chat_args(tmp_path))

    assert code == 0
    stop.assert_called_once_with(proc)


def test_run_chat_interrupt_stops_the_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _agent(tmp_path)
    proc = MagicMock()
    proc.pid = 42
    proc.poll.return_value = None
    log_path = tmp_path / "gateway.log"
    log_file = log_path.open("w+", encoding="utf-8")
    from monkeybot_cli.commands.chat import _SpawnedGateway

    spawned = _SpawnedGateway(proc=proc, log_path=log_path, log_file=log_file)
    monkeypatch.setenv("MONKEYBOT_CHAT_PLAIN", "1")

    async def session(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    with (
        patch(
            "monkeybot_cli.commands.chat.resolve_config",
            return_value=tmp_path / "monkeybot_config" / "monkeybot.yaml",
        ),
        patch("monkeybot_cli.commands.chat.load_agent_dotenv", return_value=None),
        patch("monkeybot_cli.commands.chat._occupied_gateway_message", return_value=None),
        patch("monkeybot_cli.commands.chat.is_sandbox_enabled", return_value=False),
        patch("monkeybot_cli.commands.chat._spawn_gateway", return_value=spawned),
        patch("monkeybot_cli.commands.chat._wait_for_health", return_value=True),
        patch("monkeybot_cli.commands.chat._plain_chat_session", session),
        patch("monkeybot_cli.commands.chat.stop_process_group") as stop,
    ):
        code = run_chat(_chat_args(tmp_path))

    assert code == 130
    stop.assert_called_once_with(proc)


def test_run_chat_startup_failure_stops_the_process_group(tmp_path: Path) -> None:
    _agent(tmp_path)
    proc = MagicMock()
    proc.pid = 42
    proc.poll.return_value = None
    log_path = tmp_path / "gateway.log"
    log_path.write_text("boot failed\n", encoding="utf-8")
    log_file = log_path.open("r+", encoding="utf-8")
    from monkeybot_cli.commands.chat import _SpawnedGateway

    spawned = _SpawnedGateway(proc=proc, log_path=log_path, log_file=log_file)

    with (
        patch(
            "monkeybot_cli.commands.chat.resolve_config",
            return_value=tmp_path / "monkeybot_config" / "monkeybot.yaml",
        ),
        patch("monkeybot_cli.commands.chat.load_agent_dotenv", return_value=None),
        patch("monkeybot_cli.commands.chat._occupied_gateway_message", return_value=None),
        patch("monkeybot_cli.commands.chat.is_sandbox_enabled", return_value=False),
        patch("monkeybot_cli.commands.chat._spawn_gateway", return_value=spawned),
        patch("monkeybot_cli.commands.chat._wait_for_health", return_value=False),
        patch("monkeybot_cli.commands.chat.stop_process_group") as stop,
    ):
        code = run_chat(_chat_args(tmp_path))

    assert code == 1
    stop.assert_called_once_with(proc)


def test_start_gateway_if_needed_starts_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_exec(*_args: object, **kwargs: object) -> _Proc:
        captured["kwargs"] = kwargs
        return _Proc()

    async def healthy(*_args: object, **_kwargs: object) -> bool:
        return False

    async def ready(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(gateway_manager, "_is_gateway_healthy", healthy)
    monkeypatch.setattr(gateway_manager, "_wait_for_gateway", ready)
    monkeypatch.setattr(gateway_manager, "_find_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway_manager, "_load_dotenv", lambda _path: None)
    monkeypatch.setattr(
        gateway_manager,
        "prepare_runtime_python",
        lambda *_a, **_k: RuntimePython(["python"], "cli"),
    )
    monkeypatch.setattr(gateway_manager.asyncio, "create_subprocess_exec", fake_exec)

    proc = asyncio.run(gateway_manager.start_gateway_if_needed("ws://127.0.0.1:8765"))

    assert proc is not None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    for key, value in process_tree.popen_kwargs_for_platform().items():
        assert kwargs[key] == value


def test_stop_gateway_uses_process_group_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _Proc()
    seen: list[_Proc] = []

    async def fake_stop(target: _Proc, **_kwargs: object) -> None:
        seen.append(target)

    monkeypatch.setattr(gateway_manager, "stop_process_group_async", fake_stop)
    asyncio.run(gateway_manager.stop_gateway(proc))  # type: ignore[arg-type]
    assert seen == [proc]


def _load_spike() -> object:
    path = Path(__file__).resolve().parents[1] / "spikes" / "chat_gateway_orphan_port.py"
    spec = importlib.util.spec_from_file_location("chat_gateway_orphan_port", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group teardown")
def test_chat_teardown_releases_port_held_by_shim_grandchild() -> None:
    """Same scenario as the pre-fix spike: shim exits, grandchild holds the port."""
    module = _load_spike()
    assert module.run_cases() == 0  # type: ignore[attr-defined]
