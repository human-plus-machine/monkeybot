"""T4-OPS-01: the built Docker image answers ``/health`` on 8080. Manual / nightly.

Builds ``docker/Dockerfile`` from the repo root (the build downloads packages) unless
``MONKEYBOT_DOCKER_IMAGE`` names an image that is already built. The container runs with the
compose overlay (``docker/demo.yaml``, fake provider), so it needs no credentials. Port 8080 in
the container is published on a free loopback port.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_REPO_ROOT = Path(__file__).resolve().parents[3]
_IMAGE_TAG = "monkeybot:t4-ops-01"
_CONTAINER_PORT = 8080
_BUILD_TIMEOUT_SEC = 1800
_HEALTH_TIMEOUT_SEC = 120
_DOCKER_TIMEOUT_SEC = 120


def _docker(*args: str, timeout: float = _DOCKER_TIMEOUT_SEC) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _host_port(container: str) -> int:
    mapped = _docker("port", container, f"{_CONTAINER_PORT}/tcp")
    assert mapped.returncode == 0, mapped.stderr.strip()
    return int(mapped.stdout.strip().splitlines()[0].rsplit(":", 1)[1])


def _wait_for_health(container: str, port: int) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + _HEALTH_TIMEOUT_SEC
    last = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                    return body
                last = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    logs = _docker("logs", "--tail", "50", container)
    pytest.fail(
        f"{url} did not answer 200 within {_HEALTH_TIMEOUT_SEC}s ({last})\n"
        f"{logs.stdout}{logs.stderr}"
    )


@pytest.mark.case("T4-OPS-01")
def test_docker_image_health() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    image = os.environ.get("MONKEYBOT_DOCKER_IMAGE", "").strip()
    if not image:
        image = _IMAGE_TAG
        built = _docker(
            "build", "-f", "docker/Dockerfile", "-t", image, ".", timeout=_BUILD_TIMEOUT_SEC
        )
        assert built.returncode == 0, built.stderr[-2000:]

    container = f"monkeybot-t4-ops-01-{uuid.uuid4().hex[:8]}"
    docker_dir = _REPO_ROOT / "docker"
    started = _docker(
        "run",
        "-d",
        "--name",
        container,
        "-p",
        f"127.0.0.1::{_CONTAINER_PORT}",
        "-v",
        f"{docker_dir / 'demo.yaml'}:/app/monkeybot_config/demo.yaml:ro",
        "-v",
        f"{docker_dir / 'demo-model.yaml'}:/app/monkeybot_config/demo-model.yaml:ro",
        "-e",
        "MONKEYBOT_CONFIG=/app/monkeybot_config/demo.yaml",
        "-e",
        "WEB_SEARCH_BACKEND=none",
        image,
    )
    assert started.returncode == 0, started.stderr.strip()
    try:
        body = _wait_for_health(container, _host_port(container))
        assert body.get("status") == "ok", body
    finally:
        _docker("rm", "-f", container)
