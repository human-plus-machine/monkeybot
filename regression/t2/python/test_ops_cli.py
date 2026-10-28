"""T2-OPS-01 and T2-OPS-02: ``monkeybot run`` boot and ``new`` then ``validate``."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_HEALTH_TIMEOUT_SEC = 45.0
_SHUTDOWN_TIMEOUT_SEC = 20.0


def _monkeybot() -> list[str]:
    script = Path(sys.executable).with_name("monkeybot")
    if script.is_file():
        return [str(script)]
    return [
        sys.executable,
        "-c",
        "from monkeybot_cli.main import main; raise SystemExit(main())",
    ]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return int(response.status)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _wait_healthy(port: int, proc: subprocess.Popen[str]) -> None:
    health = f"http://127.0.0.1:{port}/health"
    realtime = f"http://127.0.0.1:{port}/realtime/health"
    deadline = time.monotonic() + _HEALTH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"monkeybot run exited {proc.returncode} before health checks\n{proc.stderr}"
            )
        if _status(health) == 200 and _status(realtime) == 200:
            return
        time.sleep(0.2)
    raise AssertionError(f"health endpoints did not return 200 on port {port}\n{proc.stderr}")


def _agent_tree(root: Path) -> None:
    config = root / "monkeybot_config"
    config.mkdir()
    (config / "AGENT.md").write_text("# ops\n", encoding="utf-8")
    (root / "workspace").mkdir()
    (root / "skills").mkdir()
    (root / "data").mkdir()
    (config / "monkeybot.yaml").write_text(
        "\n".join(
            [
                "harness:",
                "  mode: turn_based",
                "model:",
                "  provider: fake",
                "  name: fake",
                "memory:",
                "  enabled: false",
                "sandbox:",
                "  enabled: false",
                "web_search:",
                "  backend: none",
                "computer:",
                "  enabled: false",
                "paths:",
                "  agent_md: ./monkeybot_config/AGENT.md",
                "  skills_path: ./skills",
                "  workspace_root: ./workspace",
                "  db_url: sqlite:///data/monkeybot.db",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.slow
@pytest.mark.case("T2-OPS-01")
def test_cli_run_boots_and_sigint_exits_130(tmp_path: Path) -> None:
    """``monkeybot run`` with ``MODEL_PROVIDER=fake`` serves both health routes and exits 130 on SIGINT."""
    _agent_tree(tmp_path)
    port = _free_port()
    env = os.environ.copy()
    env["MODEL_PROVIDER"] = "fake"
    for key in ("MONKEYBOT_CONFIG", "PORT", "GATEWAY_PORT"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [
            *_monkeybot(),
            "run",
            "--cwd",
            str(tmp_path),
            "--config",
            str(tmp_path / "monkeybot_config" / "monkeybot.yaml"),
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_healthy(port, proc)
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError("monkeybot run did not exit after SIGINT") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 130


@pytest.mark.case("T2-OPS-02")
def test_cli_new_then_validate(tmp_path: Path) -> None:
    """``monkeybot new`` then ``monkeybot validate`` passes on the scaffold."""
    dest = tmp_path / "agent"
    new = subprocess.run(
        [
            *_monkeybot(),
            "new",
            "--dest",
            str(dest),
            "--yes",
            "--provider",
            "fake",
            "--model",
            "fake",
            "--template",
            "default",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert new.returncode == 0, new.stderr or new.stdout
    validated = subprocess.run(
        [*_monkeybot(), "validate", "--cwd", str(dest)],
        cwd=dest,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert validated.returncode == 0, validated.stderr or validated.stdout


_SCRIPTED_REPLY = "scripted-gateway-reply"
_CHAT_SCRIPT = f"""\
- - type: text
    text: {_SCRIPTED_REPLY}
  - type: done
"""


def _redact(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if any(
            word in lower for word in ("token", "secret", "authorization", "api_key", "password")
        ):
            continue
        kept.append(line)
    return "\n".join(kept)[-800:]


@pytest.mark.case("T2-OPS-03")
async def test_cli_chat_prints_scripted_gateway_reply(tmp_path: Path) -> None:
    """``monkeybot chat`` one message against an in-process gateway prints the scripted reply."""
    import tempfile

    import uvicorn

    from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests
    from monkeybot.gateway.sse.app import create_app
    from testkit.gateway import (
        _DROP,
        _ISOLATION,
        _install_layout,
        _install_script,
        _pop_env,
        _push_env,
        _with_fake_provider,
    )

    with tempfile.TemporaryDirectory(prefix="mb-ops-chat-") as raw:
        root = Path(raw)
        script_path = _install_script(root, _CHAT_SCRIPT)
        _install_layout(root, _with_fake_provider(None))
        config_dir = root / "monkeybot_config"
        updates = dict(_ISOLATION)
        updates.update(
            {
                "MONKEYBOT_CONFIG": str(config_dir / "monkeybot.yaml"),
                "MONKEYBOT_AGENT_ROOT": str(root),
                "DB_URL": "sqlite:///data/monkeybot.db",
                "MONKEYBOT_FAKE_PROVIDER_SCRIPT": str(script_path),
                "AGENT_MD": str(config_dir / "AGENT.md"),
                "MCP_CONFIG": str(config_dir / "mcp.json"),
                "SKILLS_PATH": str(root / "skills"),
                "MONKEYBOT_WORKSPACE_ROOT": str(root / "workspace"),
                "MODEL_PROVIDER": "fake",
            }
        )
        previous = _push_env(updates, _DROP)
        reset_runtime_env_state_for_tests()
        app = create_app()
        port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if task.done():
                    raise AssertionError("in-process gateway exited before it bound a port")
                if time.monotonic() > deadline:
                    raise AssertionError("in-process gateway did not become ready")
                await asyncio.sleep(0.05)
            proc = await asyncio.create_subprocess_exec(
                *_monkeybot(),
                "chat",
                "--url",
                f"http://127.0.0.1:{port}",
                "--cwd",
                str(root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=root,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(tmp_path),
                    "MONKEYBOT_CHAT_PLAIN": "1",
                    "MONKEYBOT_CHAT_NO_ANIMATIONS": "1",
                },
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(b"ping the gateway\n/bye\n"),
                timeout=60,
            )
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=20)
            _pop_env(previous)
            reset_runtime_env_state_for_tests()
    text = stdout.decode()
    assert proc.returncode == 0, _redact(stderr.decode())
    assert _SCRIPTED_REPLY in text, _redact(text)
