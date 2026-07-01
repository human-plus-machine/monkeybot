"""End-to-end test for the interactive ``monkeybot chat`` subprocess."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pexpect
from monkeybot_cli.commands.chat import _wait_for_health


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_chat_repl_round_trip_with_fake_gateway(tmp_path: Path) -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Test agent\nYou are a test assistant.\n", encoding="utf-8")
    policy = tmp_path / "command_allowlist.yaml"
    policy.write_text("deny_patterns: []\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()

    env = os.environ.copy()
    env.update(
        {
            "AGENT_MD": str(agent),
            "COMMAND_ALLOWLIST_CONFIG": str(policy),
            "DB_URL": f"sqlite:///{tmp_path / 'mb.db'}",
            "MCP_CONFIG": str(tmp_path / "no_mcp.json"),
            "MEMORY_PATH": str(tmp_path / "memory"),
            "MODEL_PROVIDER": "fake",
            "PORT": str(port),
            "SKILLS_PATH": str(tmp_path / "skills"),
        }
    )

    gateway = subprocess.Popen(
        [sys.executable, "-m", "monkeybot.gateway.main"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for_health(base, gateway)
        child = pexpect.spawn(
            "monkeybot",
            ["chat", "--url", base],
            env=env,
            encoding="utf-8",
            timeout=20,
        )
        try:
            child.expect("you:")
            child.sendline("hello")
            child.expect("assistant:")
            child.expect("hello")
            child.expect("you:")
            child.sendline("/bye")
            child.expect("Goodbye")
            child.expect(pexpect.EOF)
            child.close()
            assert child.exitstatus == 0
        finally:
            child.close(force=True)
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway.kill()
            gateway.wait(timeout=5)
