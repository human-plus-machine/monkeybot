"""E2 cold-start test — verifies monkeybot serve starts and /health returns 200."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set")
def test_serve_health_check(tmp_path: Path) -> None:
    """Start monkeybot serve in subprocess, verify /health returns 200."""
    import httpx  # noqa: PLC0415

    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# TestBot\nYou are a test bot.")
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("model:\n  default: gemini-2.0-flash\n")

    port = 19876
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "monkeybot",
            "serve",
            "--bot-dir",
            str(tmp_path),
            "--port",
            str(port),
        ],
        cwd="/Users/johnpiscani/ez-ai/auriga/automation/monkeybot",
        env={**os.environ, "PYTHONPATH": "src"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://localhost:{port}/health", timeout=1.0)
                if resp.status_code == 200:
                    assert resp.json() == {"status": "ok"}
                    return
            except Exception as e:
                last_err = e
            time.sleep(0.5)
        pytest.fail(f"Server did not start within 10s. Last error: {last_err}")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
