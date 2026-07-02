"""Tests for CLI OpenSandbox lifecycle helpers."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from monkeybot_cli.opensandbox_lifecycle import (
    _inspect_container,
    host_port_from_server_url,
    is_sandbox_enabled,
    server_url_from_config,
)


def test_is_sandbox_enabled_from_yaml() -> None:
    assert is_sandbox_enabled({"sandbox": {"enabled": True}}) is True
    assert is_sandbox_enabled({"sandbox": {"enabled": False}}) is False
    assert is_sandbox_enabled({}) is False


def test_server_url_from_config() -> None:
    doc = {"sandbox": {"server_url": "http://localhost:19999"}}
    assert server_url_from_config(doc) == "http://localhost:19999"


def test_host_port_from_server_url() -> None:
    assert host_port_from_server_url("http://localhost:18080") == 18080
    assert host_port_from_server_url("http://127.0.0.1:9090/path") == 9090
    assert host_port_from_server_url("http://localhost") == 18080


def _fake_inspect_proc(payload: list[dict]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker", "container", "inspect"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_inspect_container_parses_single_docker_call() -> None:
    payload = [
        {
            "State": {"Running": True},
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]}
            },
            "Mounts": [{"Destination": "/etc/opensandbox/config.toml"}],
            "Config": {"Labels": {"mb.opensandbox.config_sha256": "abc123"}},
        }
    ]
    with patch(
        "monkeybot_cli.opensandbox_lifecycle._docker",
        return_value=_fake_inspect_proc(payload),
    ) as mock_docker:
        info = _inspect_container("monkeybot-opensandbox")

    mock_docker.assert_called_once_with("container", "inspect", "monkeybot-opensandbox")
    assert info is not None
    assert info.running is True
    assert info.published_port == "0.0.0.0:18080"
    assert info.config_mounted is True
    assert info.config_sha256 == "abc123"


def test_inspect_container_returns_none_when_missing() -> None:
    proc = subprocess.CompletedProcess(
        args=["docker", "container", "inspect"], returncode=1, stdout="", stderr="no such container"
    )
    with patch("monkeybot_cli.opensandbox_lifecycle._docker", return_value=proc):
        assert _inspect_container("missing") is None
