"""Tests for the CLI gateway manager."""

from __future__ import annotations

from pathlib import Path

import pytest

import monkeybot_cli.realtime.gateway_manager as gateway_manager
from monkeybot.cli.gateway_manager import _find_workspace_dir, _url_is_local
from monkeybot_cli.runtime_python import RuntimePython


def test_find_workspace_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text("harness:\n  mode: realtime\n")

    found = _find_workspace_dir(tmp_path)
    assert found == tmp_path


def test_find_workspace_dir_from_child(tmp_path: Path) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text("harness:\n  mode: realtime\n")

    child = tmp_path / "workspace" / "data"
    child.mkdir(parents=True)
    found = _find_workspace_dir(child)
    assert found == tmp_path


def test_find_workspace_dir_missing(tmp_path: Path) -> None:
    assert _find_workspace_dir(tmp_path) is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("ws://localhost:8080", True),
        ("ws://127.0.0.1:8080", True),
        ("wss://localhost:8080", True),
        ("ws://example.com:8080", False),
        ("wss://api.example.com/realtime", False),
        ("ws://localhost:8787", True),
    ],
)
def test_url_is_local(url: str, expected: bool) -> None:
    assert _url_is_local(url) is expected


@pytest.mark.asyncio
async def test_realtime_autostart_prepares_dotenv_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text(
        "memory:\n  enabled: false\n",
        encoding="utf-8",
    )
    alternate = config_dir / "realtime.yaml"
    alternate.write_text("memory:\n  enabled: true\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "MONKEYBOT_CONFIG=monkeybot_config/realtime.yaml\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def unhealthy(_url: str) -> bool:
        return False

    async def ready(_url: str) -> bool:
        return True

    def prepare(root: Path, config_path: Path) -> RuntimePython:
        captured["root"] = root
        captured["config_path"] = config_path
        return RuntimePython(["python"], "cli", root)

    class Process:
        returncode = None

    async def spawn(*args: str, **kwargs: object) -> Process:
        captured["argv"] = args
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    monkeypatch.setattr(gateway_manager, "_is_gateway_healthy", unhealthy)
    monkeypatch.setattr(gateway_manager, "_wait_for_gateway", ready)
    monkeypatch.setattr(gateway_manager, "prepare_runtime_python", prepare)
    monkeypatch.setattr(gateway_manager.asyncio, "create_subprocess_exec", spawn)

    await gateway_manager.start_gateway_if_needed("ws://localhost:8123")

    assert captured["root"] == tmp_path
    assert captured["config_path"] == alternate.resolve()
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MONKEYBOT_CONFIG"] == str(alternate.resolve())
