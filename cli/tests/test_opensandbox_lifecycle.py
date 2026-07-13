"""Tests for CLI OpenSandbox lifecycle helpers."""

from __future__ import annotations

from monkeybot_cli.opensandbox_lifecycle import (
    _DEFAULT_SERVER_IMAGE,
    _server_image,
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


def test_server_image_ignores_worker_sandbox_image(monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_IMAGE", "monkeybot-sandbox:local")
    monkeypatch.delenv("SANDBOX_SERVER_IMAGE", raising=False)
    monkeypatch.delenv("OPENSANDBOX_SERVER_IMAGE", raising=False)
    assert _server_image() == _DEFAULT_SERVER_IMAGE


def test_server_image_prefers_explicit_server_env(monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_IMAGE", "monkeybot-sandbox:local")
    monkeypatch.setenv("SANDBOX_SERVER_IMAGE", "opensandbox/server:v9")
    assert _server_image() == "opensandbox/server:v9"
