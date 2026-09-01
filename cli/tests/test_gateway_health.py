"""Tests for gateway health / occupied-port guards."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from monkeybot_cli.commands.chat import (
    _spawn_gateway,
    _SpawnedGateway,
    run_chat,
)
from monkeybot_cli.gateway_health import occupied_gateway_message, wait_for_health
from monkeybot_cli.runtime_python import RuntimePython


def test_occupied_gateway_message_none_when_free() -> None:
    with (
        patch("monkeybot_cli.gateway_health.port_free", return_value=True),
        patch("monkeybot_cli.gateway_health.health_ok", return_value=False),
    ):
        assert occupied_gateway_message("http://127.0.0.1:8080", 8080) is None


def test_occupied_gateway_message_when_port_taken() -> None:
    with (
        patch("monkeybot_cli.gateway_health.port_free", return_value=False),
        patch("monkeybot_cli.gateway_health.health_ok", return_value=True),
    ):
        msg = occupied_gateway_message("http://127.0.0.1:8080", 8080)
    assert msg is not None
    assert "8080" in msg
    assert "--attach" in msg
    assert "/bye" in msg


def test_wait_for_health_rejects_dead_child_after_stale_200() -> None:
    """Health 200 from a pre-existing server must not win if our spawn exited."""
    proc = MagicMock()
    # Alive on the pre-check, dead after the successful health response.
    proc.poll.side_effect = [None, 1]

    class _Resp:
        status_code = 200

    with patch("monkeybot_cli.gateway_health.httpx.get", return_value=_Resp()):
        assert wait_for_health("http://127.0.0.1:18080", proc, timeout_s=1.0) is False


def test_spawn_gateway_does_not_set_transcript_env(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_popen(*args: object, **kwargs: object) -> object:
        captured["env"] = kwargs["env"]
        return object()

    monkeypatch.delenv("MONKEYBOT_TRANSCRIPT_ENABLED", raising=False)
    monkeypatch.setattr(
        "monkeybot_cli.commands.chat.prepare_runtime_python",
        lambda *a, **k: RuntimePython(["python"], "cli"),
    )
    monkeypatch.setattr("monkeybot_cli.commands.chat.subprocess.Popen", fake_popen)
    spawned = _spawn_gateway(None, tmp_path, 8123)
    try:
        env = captured["env"]
        assert isinstance(env, dict)
        assert "MONKEYBOT_TRANSCRIPT_ENABLED" not in env
    finally:
        spawned.log_file.close()
        spawned.log_path.unlink()


def test_run_chat_refuses_occupied_port(tmp_path: Path, capsys) -> None:
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: gemini\n  name: test\nruntime:\n  port: 18080\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
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
        no_animations=True,
        theme="auto",
    )
    spawn = MagicMock(side_effect=AssertionError("must not spawn when port occupied"))

    with (
        patch(
            "monkeybot_cli.commands.chat.resolve_config",
            return_value=cfg_dir / "monkeybot.yaml",
        ),
        patch("monkeybot_cli.commands.chat.load_agent_dotenv", return_value=None),
        patch(
            "monkeybot_cli.commands.chat._occupied_gateway_message",
            return_value="Port 18080 is already in use. use --attach",
        ),
        patch("monkeybot_cli.commands.chat._spawn_gateway", spawn),
    ):
        code = run_chat(args)

    assert code == 1
    err = capsys.readouterr().err
    assert "18080" in err
    assert "--attach" in err
    spawn.assert_not_called()


def test_run_chat_spawns_when_port_free(tmp_path: Path, capsys) -> None:
    """Smoke: occupied check passes, then existing failed-health path still works."""
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: gemini\n  name: test\nruntime:\n  port: 18080\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "gateway.log"
    log_path.write_text("boot failed\n", encoding="utf-8")
    log_file = log_path.open("r+", encoding="utf-8")
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.wait.return_value = 1
    spawned = _SpawnedGateway(proc=proc, log_path=log_path, log_file=log_file)
    args = argparse.Namespace(
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
        no_animations=True,
        theme="auto",
    )

    with (
        patch(
            "monkeybot_cli.commands.chat.resolve_config",
            return_value=cfg_dir / "monkeybot.yaml",
        ),
        patch("monkeybot_cli.commands.chat.load_agent_dotenv", return_value=None),
        patch("monkeybot_cli.commands.chat._occupied_gateway_message", return_value=None),
        patch("monkeybot_cli.commands.chat._spawn_gateway", return_value=spawned),
        patch("monkeybot_cli.commands.chat._wait_for_health", return_value=False),
    ):
        code = run_chat(args)

    assert code == 1
    assert "Gateway failed to start." in capsys.readouterr().err
