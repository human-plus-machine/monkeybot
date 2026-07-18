"""Start OpenSandbox server for ``monkeybot chat`` when sandbox is enabled in config."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

_DEFAULT_SERVER_IMAGE = "opensandbox/server:latest"
_DEFAULT_CONTAINER = "monkeybot-opensandbox"
# Docker Desktop / first image pull can take well over a few seconds.
_DEFAULT_DOCKER_WAIT_SECS = 60.0
_DEFAULT_HEALTH_WAIT_SECS = 60.0


def sandbox_section(doc: dict) -> dict:
    sec = doc.get("sandbox")
    return sec if isinstance(sec, dict) else {}


def is_sandbox_enabled(doc: dict) -> bool:
    sec = sandbox_section(doc)
    enabled = sec.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    raw = os.environ.get("SANDBOX_ENABLED", "").strip().lower()
    return raw in {"true", "1", "on", "yes"}


def server_url_from_config(doc: dict) -> str:
    sec = sandbox_section(doc)
    url = sec.get("server_url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    env_url = os.environ.get("SANDBOX_SERVER_URL", "").strip()
    if env_url:
        return env_url
    return "http://localhost:18080"


def host_port_from_server_url(server_url: str, *, default: int = 18080) -> int:
    parsed = urlparse(server_url)
    if parsed.port is not None:
        return parsed.port
    return default


def _config_sha256(config_path: Path) -> str:
    if not config_path.is_file():
        return ""
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return digest


def _health_ok(host_port: int) -> bool:
    url = f"http://127.0.0.1:{host_port}/health"
    try:
        resp = httpx.get(url, timeout=2.0)
        body = resp.text
    except httpx.HTTPError:
        return False
    return '"status"' in body and "healthy" in body


def _server_image() -> str:
    """OpenSandbox *control plane* image (not the session/worker ``SANDBOX_IMAGE``).

    ``sandbox.image`` / ``SANDBOX_IMAGE`` is the container image for agent
    ``run_command`` sessions. Reusing it here starts a worker that exits
    immediately and then burns the full health-wait timeout.
    """
    for key in ("SANDBOX_SERVER_IMAGE", "OPENSANDBOX_SERVER_IMAGE"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return raw
    return _DEFAULT_SERVER_IMAGE


def _wait_healthy(
    host_port: int,
    *,
    timeout_secs: float,
    container: str | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if _health_ok(host_port):
            return True
        # Dead container will never become healthy — don't burn the full wait.
        if container and _container_exists(container) and not _container_running(container):
            return False
        time.sleep(0.5)
    return False


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _docker_available() -> bool:
    try:
        proc = _docker("info")
    except OSError:
        return False
    return proc.returncode == 0


def _wait_docker_available(*, timeout_secs: float) -> bool:
    """Retry ``docker info`` until the daemon is up (Docker Desktop cold start)."""
    if timeout_secs <= 0:
        return _docker_available()
    if _docker_available():
        return True
    print(
        f"monkeybot chat: waiting up to {timeout_secs:.0f}s for Docker…",
        flush=True,
    )
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if _docker_available():
            return True
    return False


def _container_running(name: str) -> bool:
    proc = _docker("container", "inspect", "-f", "{{.State.Running}}", name)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _container_exists(name: str) -> bool:
    proc = _docker("container", "inspect", name)
    return proc.returncode == 0


def _published_port(name: str) -> str:
    proc = _docker("port", name, "8080/tcp")
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _config_mount_ok(name: str) -> bool:
    proc = _docker(
        "container",
        "inspect",
        name,
        "--format",
        "{{range .Mounts}}{{.Destination}};{{end}}",
    )
    if proc.returncode != 0:
        return False
    return "/etc/opensandbox/config.toml" in proc.stdout


def _container_config_label(name: str) -> str:
    proc = _docker(
        "container",
        "inspect",
        name,
        "--format",
        '{{index .Config.Labels "mb.opensandbox.config_sha256"}}',
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _remove_container(name: str) -> None:
    _docker("rm", "-f", name)


def _start_existing(name: str) -> None:
    _docker("start", name)


def _run_container(
    *,
    name: str,
    host_port: int,
    config_path: Path,
    image: str,
    cfg_hash: str,
) -> bool:
    proc = _docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"mb.opensandbox.config_sha256={cfg_hash}",
        "--add-host=host.docker.internal:host-gateway",
        "-p",
        f"{host_port}:8080",
        "-e",
        "OPENSANDBOX_INSECURE_SERVER=YES",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{config_path}:/etc/opensandbox/config.toml:ro",
        image,
    )
    return proc.returncode == 0


def resolve_opensandbox_config(agent_root: Path) -> Path:
    return (agent_root / "monkeybot_config" / "opensandbox.docker.toml").resolve()


def ensure_opensandbox_for_agent(
    agent_root: Path,
    *,
    server_url: str,
    skip: bool | None = None,
    docker_wait_secs: float | None = None,
    health_wait_secs: float | None = None,
) -> bool:
    """Ensure OpenSandbox server is reachable; start docker container when needed.

    Returns True when health check passes or sandbox is skipped/disabled upstream.

    Pass a small ``docker_wait_secs`` (e.g. 2) from ``monkeybot run`` so missing
    Docker Desktop cannot block gateway startup for tens of seconds.
    """
    if skip is None:
        skip = os.environ.get("SKIP_OPENSANDBOX", "").strip() == "1"
    if skip:
        return True

    host_port = host_port_from_server_url(server_url)
    if _health_ok(host_port):
        return True

    if docker_wait_secs is None:
        docker_wait_secs = float(
            os.environ.get("SANDBOX_DOCKER_WAIT_SECS", str(_DEFAULT_DOCKER_WAIT_SECS))
        )
    if not _wait_docker_available(timeout_secs=docker_wait_secs):
        print(
            "monkeybot: Docker not available; run_command sandbox will fail "
            "(set sandbox.enabled: false or start Docker).",
            flush=True,
        )
        return False

    config_path = resolve_opensandbox_config(agent_root)
    if not config_path.is_file():
        print(
            f"monkeybot: missing {config_path}; cannot start OpenSandbox.",
            flush=True,
        )
        return False

    container = os.environ.get("SANDBOX_CONTAINER", _DEFAULT_CONTAINER).strip() or _DEFAULT_CONTAINER
    image = _server_image()
    if health_wait_secs is None:
        health_wait_secs = float(
            os.environ.get("SANDBOX_HEALTH_WAIT_SECS", str(_DEFAULT_HEALTH_WAIT_SECS))
        )
    wait_secs = health_wait_secs
    want_hash = _config_sha256(config_path)

    if _container_exists(container):
        published = _published_port(container)
        port_ok = f":{host_port}" in published
        if not _config_mount_ok(container) or not port_ok:
            print(f"monkeybot: recreating OpenSandbox container {container}", flush=True)
            _remove_container(container)
        elif want_hash and _container_config_label(container) != want_hash:
            print("monkeybot: recreating OpenSandbox (config changed)", flush=True)
            _remove_container(container)
        elif not _container_running(container):
            print(f"monkeybot: starting OpenSandbox container {container}", flush=True)
            _start_existing(container)

    if _container_exists(container):
        if _wait_healthy(host_port, timeout_secs=wait_secs, container=container):
            return True
        print("monkeybot: OpenSandbox unhealthy; recreating container", flush=True)
        _remove_container(container)

    print(
        f"monkeybot: starting OpenSandbox ({image}, port {host_port})…",
        flush=True,
    )
    if not _run_container(
        name=container,
        host_port=host_port,
        config_path=config_path,
        image=image,
        cfg_hash=want_hash,
    ):
        print(
            f"monkeybot: docker run failed (is port {host_port} in use?)",
            flush=True,
        )
        return False

    if _wait_healthy(host_port, timeout_secs=wait_secs, container=container):
        print(f"monkeybot: OpenSandbox ready (127.0.0.1:{host_port})", flush=True)
        return True

    if _container_exists(container) and not _container_running(container):
        print(
            "monkeybot: OpenSandbox container exited before becoming healthy "
            f"(image={image}).",
            flush=True,
        )
    else:
        print(
            f"monkeybot: OpenSandbox did not become healthy within {wait_secs:.0f}s.",
            flush=True,
        )
    return False
