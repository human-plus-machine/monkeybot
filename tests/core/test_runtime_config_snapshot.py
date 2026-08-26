"""Tests for the immutable RuntimeConfig snapshot and ConfigStore (hot-reload Step 1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from monkeybot.core.config import (
    ConfigTier,
    apply_monkeybot_runtime_env,
    get_config_store,
    reset_runtime_env_state_for_tests,
)
from monkeybot.core.config.runtime_env import ENV_FIELD_PATHS, ENV_MAP, ENV_TIERS
from monkeybot.core.config.snapshot import build_runtime_config, env_field_value


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


def test_env_map_three_way_exhaustiveness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env_names = set(ENV_MAP.values())
    assert len(ENV_MAP) == len(env_names)
    assert set(ENV_TIERS) == env_names
    assert set(ENV_FIELD_PATHS) == env_names
    cfg = build_runtime_config(agent_root=tmp_path)
    for env_name in sorted(env_names):
        assert ENV_TIERS[env_name] in ConfigTier
        env_field_value(cfg, env_name)


def test_pinned_env_beats_yaml_on_build_and_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    first_revision = first.revision

    yaml_path.write_text(
        "model:\n  name: yaml-after-reload\nruntime:\n  port: 2222\n",
        encoding="utf-8",
    )
    reloaded, diff = store.reload()
    assert not diff.noop
    assert reloaded.revision == first_revision + 1
    assert reloaded.model.name == "from-env"
    assert reloaded.gateway.port == "2222"
    # Step 1 does not re-mutate process env on reload.
    assert os.environ.get("MODEL_NAME") == "from-env"
    assert os.environ.get("PORT") == "1111"


def test_reload_unchanged_files_is_digest_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path, "model:\n  name: stable\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    apply_monkeybot_runtime_env()
    store = get_config_store()
    first = store.current()

    reloaded, diff = store.reload()
    assert diff.noop
    assert reloaded is first
    assert reloaded.revision == first.revision
    assert reloaded.digest == first.digest


def test_editing_monkeybot_yaml_bumps_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    yaml_path = _write_yaml(tmp_path, "model:\n  name: before\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    apply_monkeybot_runtime_env()
    store = get_config_store()
    first = store.current()

    yaml_path.write_text("model:\n  name: after\n", encoding="utf-8")
    reloaded, diff = store.reload()
    assert not diff.noop
    assert reloaded.revision == first.revision + 1
    assert reloaded.digest != first.digest
    assert reloaded.model.name == "after"
    assert "MODEL_NAME" in diff.changed_env_keys
    assert ConfigTier.HOT in diff.tiers


def test_editing_agent_md_content_bumps_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path, "runtime:\n  log_level: INFO\n")
    agent_md = tmp_path / "monkeybot_config" / "AGENT.md"
    agent_md.write_text("persona v1\n", encoding="utf-8")
    apply_monkeybot_runtime_env()
    store = get_config_store()
    first = store.current()
    assert first.paths.agent_md_digest is not None

    agent_md.write_text("persona v2\n", encoding="utf-8")
    reloaded, diff = store.reload()
    assert not diff.noop
    assert reloaded.revision == first.revision + 1
    assert reloaded.paths.agent_md_digest != first.paths.agent_md_digest
    assert "agent_md" in diff.changed_content
    assert ConfigTier.HOT in diff.tiers


def test_apply_second_call_is_digest_noop_returns_path(
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


def test_reload_does_not_mutate_pinned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _write_yaml(tmp_path, "model:\n  name: first-model\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    apply_monkeybot_runtime_env(config_path=path, agent_root=tmp_path)
    pinned = get_config_store().current()
    assert pinned.model.name == "first-model"

    path.write_text("model:\n  name: second-model\n", encoding="utf-8")
    updated, diff = get_config_store().reload(config_path=path, agent_root=tmp_path)
    assert not diff.noop
    assert updated.model.name == "second-model"
    assert pinned.model.name == "first-model"
    assert pinned.revision != updated.revision


def test_env_value_prefers_pinned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_turn_context_keeps_pinned_config_across_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.context import TurnContext
    from monkeybot.core.types.types_tools import ToolDef

    monkeypatch.chdir(tmp_path)
    path = _write_yaml(tmp_path, "model:\n  name: turn-a\n")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    apply_monkeybot_runtime_env(config_path=path, agent_root=tmp_path)
    pinned = get_config_store().current()
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model=pinned.model.name or "gemini-2.5-flash",
        config=pinned,
    )
    path.write_text("model:\n  name: turn-b\n", encoding="utf-8")
    get_config_store().reload(config_path=path, agent_root=tmp_path)
    assert ctx.config is pinned
    assert ctx.config.model.name == "turn-a"
    assert get_config_store().current().model.name == "turn-b"


def test_subagent_settings_included_in_snapshot_and_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config.settings import get_subagent_settings

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
    # A turn holding the pinned snapshot keeps its subagent settings even
    # after the file changes and the store reloads to a new revision.
    assert get_subagent_settings(config=pinned).max_turns == 5

    yaml_path.write_text(
        "model:\n  provider: fake\n"
        "subagents:\n  timeout_sec: 60\n  max_turns: 9\n  vertex_google_search: false\n",
        encoding="utf-8",
    )
    reloaded, diff = get_config_store().reload(config_path=yaml_path, agent_root=tmp_path)
    assert not diff.noop
    assert "subagents.*" in diff.changed_env_keys
    assert ConfigTier.REBUILD in diff.tiers
    assert reloaded.subagent_settings.max_turns == 9
    assert get_subagent_settings(config=pinned).max_turns == 5
    assert get_subagent_settings(config=reloaded).max_turns == 9


def test_effective_max_turns_uses_pinned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.loop_usage import _effective_max_turns

    monkeypatch.chdir(tmp_path)
    yaml_path = _write_yaml(tmp_path, "model:\n  max_turns: 7\n")
    monkeypatch.delenv("MAX_TURNS", raising=False)
    apply_monkeybot_runtime_env(config_path=yaml_path, agent_root=tmp_path)
    pinned = get_config_store().current()
    (tmp_path / "monkeybot_config" / "monkeybot.yaml").write_text(
        "model:\n  max_turns: 99\n",
        encoding="utf-8",
    )
    get_config_store().reload()
    assert _effective_max_turns(None, pinned) == 7
    assert _effective_max_turns(None, get_config_store().current()) == 99
