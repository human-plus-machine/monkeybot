"""Cold start timing tests — CI-gated performance checks."""
from __future__ import annotations

import subprocess
import time


def test_import_time() -> None:
    """import monkeybot must complete in < 200ms (2000ms including uv startup)."""
    start = time.monotonic()
    result = subprocess.run(
        ["uv", "run", "python", "-c", "import monkeybot"],
        capture_output=True,
        timeout=10,
        cwd="/Users/johnpiscani/ez-ai/auriga/automation/monkeybot",
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0, result.stderr.decode()
    assert elapsed_ms < 2000, f"Import took {elapsed_ms:.0f}ms (limit: 2000ms including uv startup)"


def test_cli_startup_time() -> None:
    """monkeybot --help must start in < 500ms (5000ms including uv startup)."""
    start = time.monotonic()
    result = subprocess.run(
        ["uv", "run", "monkeybot", "--help"],
        capture_output=True,
        timeout=10,
        cwd="/Users/johnpiscani/ez-ai/auriga/automation/monkeybot",
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0, result.stderr.decode()
    assert elapsed_ms < 5000, (
        f"CLI startup took {elapsed_ms:.0f}ms (limit: 5000ms including uv startup)"
    )
