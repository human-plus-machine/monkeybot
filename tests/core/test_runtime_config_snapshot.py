"""Tests for the immutable RuntimeConfig snapshot and ConfigStore."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from monkeybot.core.config import (
    apply_monkeybot_runtime_env,
    get_config_store,
    reset_runtime_env_state_for_tests,
)
from monkeybot.core.config.runtime_env import ENV_MAP


@pytest.fixture(autouse=True)
def _reset_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    env_before = {k: os.environ.get(k) for k in ENV_MAP.values()}
    yield
    reset_runtime_env_state_for_tests()
    for key, before_val in env_before.items():
        if before_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before_val


def _write_yaml(tmp_path: Path, body: str) -> Path:
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir(exist_ok=True)
    path = cfg_dir / "monkeybot.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_pinned_env_beats_yaml_on_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    yaml_path = _write_yaml(tmp_path, "model:\n  name: from-yaml\nruntime:\n  port: 1111\n")
    monkeypatch.setenv("MODEL_NAME", "from-env")
    monkeypatch.delenv("PORT", raising=False)

    applied = apply_monkeybot_runtime_env()
    assert applied == yaml_path.resolve()
    store = get_config_store()
    first = store.current()
    assert first.model.name == "from-env"
    assert first.gateway.port == "1111"
    assert os.environ.get("MODEL_NAME") == "from-env"
    assert os.environ.get("PORT") == "1111"


def test_apply_second_call_keeps_revision_and_returns_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    yaml_path = _write_yaml(tmp_path, "runtime:\n  port: 3333\n")
    monkeypatch.delenv("PORT", raising=False)
    first = apply_monkeybot_runtime_env()
    assert first == yaml_path.resolve()
    assert apply_monkeybot_runtime_env() == yaml_path.resolve()
    assert os.environ.get("PORT") == "3333"
    assert get_config_store().current().revision == 1


def test_apply_sets_same_env_keys_as_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_yaml(
        tmp_path,
        "runtime:\n  port: 9191\n"
        "model:\n  name: test-model-x\n  provider: fake\n"
        "tools:\n  denied_patterns:\n    - one\n    - two\n"
        "computer:\n  enabled: true\n"
        "paths:\n  workspace_root: ./agent-ws\n  agent_id: my-stable-agent\n"
        "gcp:\n  project_id: yaml-placeholder\n"
        "anthropic_vertex:\n  project_id: yaml-placeholder\n"
        "harness:\n  mode: realtime\n"
        "realtime:\n"
        "  websocket:\n    enabled: true\n    port: 9090\n"
        "  session:\n    max_duration_sec: 900\n"
        "  metrics:\n    emit_summary_on_close: false\n",
    )
    monkeypatch.setenv("PORT", "2222")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    for key in (
        "MODEL_NAME",
        "MODEL_PROVIDER",
        "MONKEYBOT_TOOL_DENIED_PATTERNS",
        "MONKEYBOT_COMPUTER_TOOLS",
        "MONKEYBOT_WORKSPACE_ROOT",
        "MONKEYBOT_AGENT_ID",
        "VERTEX_AI_PROJECT_ID",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "MONKEYBOT_HARNESS_MODE",
        "MONKEYBOT_REALTIME_WS_ENABLED",
        "MONKEYBOT_REALTIME_WS_PORT",
        "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC",
        "MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE",
    ):
        monkeypatch.delenv(key, raising=False)

    apply_monkeybot_runtime_env()

    assert os.environ.get("PORT") == "2222"
    assert os.environ.get("MODEL_NAME") == "test-model-x"
    assert os.environ.get("MODEL_PROVIDER") == "fake"
    assert os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS") == "one,two"
    assert os.environ.get("MONKEYBOT_COMPUTER_TOOLS") == "true"
    assert os.environ.get("MONKEYBOT_WORKSPACE_ROOT") == str((tmp_path / "agent-ws").resolve())
    assert os.environ.get("MONKEYBOT_AGENT_ID") == "my-stable-agent"
    assert os.environ.get("VERTEX_AI_PROJECT_ID") == "from-env"
    assert os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") == "from-env"
    assert os.environ.get("MONKEYBOT_HARNESS_MODE") == "realtime"
    assert os.environ.get("MONKEYBOT_REALTIME_WS_ENABLED") == "true"
    assert os.environ.get("MONKEYBOT_REALTIME_WS_PORT") == "9090"
    assert os.environ.get("MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC") == "900"
    assert os.environ.get("MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE") == "false"

    cfg = get_config_store().current()
    assert cfg.gateway.port == "2222"
    assert cfg.model.name == "test-model-x"
    assert cfg.realtime.websocket.port == 9090
    assert cfg.realtime.enabled is True


def test_workspace_root_legacy_env_blocks_yaml_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path, "paths:\n  workspace_root: ./from-yaml\n")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "legacy-ws"))
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    apply_monkeybot_runtime_env()
    assert "MONKEYBOT_WORKSPACE_ROOT" not in os.environ
    assert get_config_store().current().paths.workspace_root is None


def test_current_raises_before_apply() -> None:
    with pytest.raises(RuntimeError, match="has not been loaded"):
        get_config_store().current()


def test_current_or_none_before_apply() -> None:
    assert get_config_store().current_or_none() is None


def test_env_value_prefers_pinned_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.config.snapshot import env_value

    monkeypatch.chdir(tmp_path)
    path = _write_yaml(tmp_path, "model:\n  name: snap-name\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    apply_monkeybot_runtime_env(config_path=path, agent_root=tmp_path)
    cfg = get_config_store().current()
    monkeypatch.setenv("MODEL_NAME", "env-later")
    assert env_value(cfg, "MODEL_NAME") == "snap-name"
    assert env_value(None, "MODEL_NAME") == "env-later"


def test_env_value_missing_snapshot_key_does_not_read_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config.snapshot import current_env_or_none, env_value

    monkeypatch.chdir(tmp_path)
    path = _write_yaml(tmp_path, "model:\n  name: snap-name\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
    apply_monkeybot_runtime_env(config_path=path, agent_root=tmp_path)
    cfg = get_config_store().current()
    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    assert "SANDBOX_ENABLED" not in cfg.env_values
    assert env_value(cfg, "SANDBOX_ENABLED", "false") == "false"
    assert env_value(None, "SANDBOX_ENABLED", "false") == "true"
    assert current_env_or_none("SANDBOX_ENABLED") is None


def test_context_window_tokens_warns_on_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from monkeybot.core.config.snapshot import context_window_tokens

    monkeypatch.chdir(tmp_path)
    path = _write_yaml(tmp_path, "model:\n  context_window: not-a-number\n")
    monkeypatch.delenv("MODEL_CONTEXT_WINDOW", raising=False)
    apply_monkeybot_runtime_env(config_path=path, agent_root=tmp_path)
    cfg = get_config_store().current()
    with caplog.at_level("WARNING"):
        assert context_window_tokens(cfg) == 200_000
    assert "invalid MODEL_CONTEXT_WINDOW" in caplog.text


def test_current_env_uses_snapshot_then_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config.snapshot import current_env

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODEL_NAME", "from-env")
    assert current_env("MODEL_NAME") == "from-env"
    _write_yaml(tmp_path, "model:\n  name: from-yaml\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    apply_monkeybot_runtime_env()
    assert current_env("MODEL_NAME") == "from-yaml"
    monkeypatch.setenv("MODEL_NAME", "env-after-apply")
    assert current_env("MODEL_NAME") == "from-yaml"


def test_subagent_settings_included_in_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    yaml_path = _write_yaml(
        tmp_path,
        "model:\n  provider: fake\n"
        "subagents:\n  timeout_sec: 30\n  max_turns: 5\n  vertex_google_search: true\n",
    )
    apply_monkeybot_runtime_env(config_path=yaml_path, agent_root=tmp_path)
    pinned = get_config_store().current()
    assert pinned.subagent_settings.timeout_sec == 30.0
    assert pinned.subagent_settings.max_turns == 5
    assert pinned.subagent_settings.vertex_google_search is True
