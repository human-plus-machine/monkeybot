"""Tests for ``monkeybot.core.config.runtime_env``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from monkeybot.core.config import runtime_env


@pytest.fixture(autouse=True)
def _reset_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_env.reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    # Snapshot all env vars that apply_monkeybot_runtime_env() may set so we can
    # restore them after the test. monkeypatch.delenv is a no-op (no undo registered)
    # when the key is absent, so direct os.environ writes inside the tested function
    # would otherwise leak across tests.
    env_before = {k: os.environ.get(k) for k in runtime_env._ENV_MAP.values()}
    yield
    runtime_env.reset_runtime_env_state_for_tests()
    for key, before_val in env_before.items():
        if before_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before_val


def test_yaml_applies_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "runtime:\n  port: 9191\nmodel:\n  name: test-model-x\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("PORT") == "9191"
    assert os.environ.get("MODEL_NAME") == "test-model-x"


def test_scheduler_enabled_yaml_applies_and_enables_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.scheduler.engine import scheduler_enabled_from_env

    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "scheduler:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MONKEYBOT_SCHEDULER_ENABLED", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_SCHEDULER_ENABLED") == "true"
    assert scheduler_enabled_from_env() is True


def test_scheduler_enabled_explicit_env_wins_over_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.scheduler.engine import scheduler_enabled_from_env

    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "scheduler:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MONKEYBOT_SCHEDULER_ENABLED", "false")
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_SCHEDULER_ENABLED") == "false"
    assert scheduler_enabled_from_env() is False


def test_google_cloud_project_wins_over_yaml_gcp_project_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "gcp:\n  project_id: yaml-placeholder\n"
        "anthropic_vertex:\n  project_id: yaml-placeholder\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    monkeypatch.delenv("VERTEX_AI_PROJECT_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("VERTEX_AI_PROJECT_ID") == "from-env"
    assert os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") == "from-env"


def test_existing_env_wins_over_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "runtime:\n  port: 7777\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PORT", "2222")
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("PORT") == "2222"


def test_monkeybot_config_explicit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    y = tmp_path / "custom.yaml"
    y.write_text(
        "model:\n  provider: fake\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MONKEYBOT_CONFIG", str(y))
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MODEL_PROVIDER") == "fake"


def test_custom_config_paths_anchor_at_its_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "custom" / "agent.yaml"
    config.parent.mkdir()
    config.write_text(
        "paths:\n"
        "  workspace_root: workspace\n"
        "  db_url: sqlite:///data/monkeybot.db\n"
        "  memory_storage_uri: local://memory\n",
        encoding="utf-8",
    )
    for key in ("MONKEYBOT_WORKSPACE_ROOT", "DB_URL", "MEMORY_STORAGE_URI"):
        monkeypatch.delenv(key, raising=False)

    runtime_env.apply_monkeybot_runtime_env(config_path=config)

    assert os.environ["MONKEYBOT_WORKSPACE_ROOT"] == str(config.parent / "workspace")
    assert os.environ["DB_URL"] == f"sqlite:///{config.parent / 'data' / 'monkeybot.db'}"
    assert os.environ["MEMORY_STORAGE_URI"] == f"local://{config.parent / 'memory'}"


def test_includes_merge_overrides_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    inc = cfg_dir / "includes"
    inc.mkdir()
    (inc / "b.yaml").write_text(
        "model:\n  name: from-include\n",
        encoding="utf-8",
    )
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  name: from-base\nincludes:\n  - includes/b.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MODEL_NAME", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MODEL_NAME") == "from-include"


def test_gateway_cors_allow_origins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "gateway:\n  cors_allow_origins: \"http://a.example,http://b.example\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MONKEYBOT_CORS_ALLOW_ORIGINS", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_CORS_ALLOW_ORIGINS") == "http://a.example,http://b.example"


def test_paths_workspace_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "paths:\n  workspace_root: ./agent-ws\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_WORKSPACE_ROOT") == str((tmp_path / "agent-ws").resolve())


def test_model_summarization_model_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  summarization_model: flash-lite\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CONTEXT_SUMMARIZATION_MODEL", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("CONTEXT_SUMMARIZATION_MODEL") == "flash-lite"


def test_memory_storage_uri_sets_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "paths:\n  memory_storage_uri: gcs://my-bucket/mem\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MEMORY_STORAGE_URI", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MEMORY_STORAGE_URI") == "gcs://my-bucket/mem"


def test_legacy_memory_path_still_sets_memory_path_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "paths:\n  memory_path: ./legacy/mem\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MEMORY_PATH", raising=False)
    monkeypatch.delenv("MEMORY_STORAGE_URI", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MEMORY_PATH") == str((tmp_path / "legacy" / "mem").resolve())
    assert os.environ.get("MEMORY_STORAGE_URI") is None


def test_tools_budget_env_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "tools:\n"
        "  read_max_lines: 4000\n"
        "  read_default_lines: 1500\n"
        "  spill_read_max_lines: 25000\n"
        "  spill_min_chars: 9000\n"
        "  result_budget_fraction: 0.75\n"
        "  result_budget_floor_tokens: 1500\n",
        encoding="utf-8",
    )
    for key in (
        "MONKEYBOT_READ_MAX_LINES",
        "MONKEYBOT_READ_DEFAULT_LINES",
        "MONKEYBOT_SPILL_READ_MAX_LINES",
        "MONKEYBOT_SPILL_MIN_CHARS",
        "MONKEYBOT_RESULT_BUDGET_FRACTION",
        "MONKEYBOT_RESULT_BUDGET_FLOOR_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    runtime_env.reset_runtime_env_state_for_tests()
    runtime_env.apply_monkeybot_runtime_env()
    # read_* / spill_* are no longer env-mapped (YAML-only or retired).
    assert os.environ.get("MONKEYBOT_READ_MAX_LINES") is None
    assert os.environ.get("MONKEYBOT_READ_DEFAULT_LINES") is None
    assert os.environ.get("MONKEYBOT_SPILL_READ_MAX_LINES") is None
    assert os.environ.get("MONKEYBOT_SPILL_MIN_CHARS") is None
    # Result-budget / compression ratios are harness-fixed (not YAML/env knobs).
    assert os.environ.get("MONKEYBOT_RESULT_BUDGET_FRACTION") is None
    assert os.environ.get("MONKEYBOT_RESULT_BUDGET_FLOOR_TOKENS") is None
    # Retired spill keys warn but do not reject.
    found = runtime_env.warn_retired_tools_keys(
        {
            "tools": {
                "spill_min_chars": 9000,
                "spill_read_max_lines": 25000,
            }
        }
    )
    assert found == ["spill_min_chars", "spill_read_max_lines"]


def test_compression_ratios_not_mapped_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "compression:\n"
        "  light_ratio: 0.1\n"
        "  moderate_ratio: 0.2\n"
        "  aggressive_ratio: 0.3\n",
        encoding="utf-8",
    )
    for key in (
        "MONKEYBOT_PRESSURE_LIGHT_RATIO",
        "MONKEYBOT_PRESSURE_MODERATE_RATIO",
        "MONKEYBOT_PRESSURE_AGGRESSIVE_RATIO",
    ):
        monkeypatch.delenv(key, raising=False)
    runtime_env.reset_runtime_env_state_for_tests()
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_PRESSURE_LIGHT_RATIO") is None
    assert os.environ.get("MONKEYBOT_PRESSURE_MODERATE_RATIO") is None
    assert os.environ.get("MONKEYBOT_PRESSURE_AGGRESSIVE_RATIO") is None


def test_denied_patterns_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "tools:\n  denied_patterns:\n    - one\n    - two\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MONKEYBOT_TOOL_DENIED_PATTERNS", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_TOOL_DENIED_PATTERNS") == "one,two"


def test_apply_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "runtime:\n  port: 3333\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PORT", raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("PORT") == "3333"


def test_missing_explicit_config_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONKEYBOT_CONFIG", "/nonexistent/monkeybot.yaml")
    assert runtime_env.apply_monkeybot_runtime_env() is None


def test_invalid_yaml_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text("runtime: [", encoding="utf-8")
    with pytest.raises((yaml.YAMLError, ValueError)):
        runtime_env.apply_monkeybot_runtime_env()


def test_realtime_nested_keys_flattened_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "harness:\n  mode: realtime\n"
        "realtime:\n"
        "  websocket:\n    enabled: true\n    port: 9090\n"
        "  session:\n    max_duration_sec: 900\n"
        "  metrics:\n    emit_summary_on_close: false\n",
        encoding="utf-8",
    )
    for key in (
        "MONKEYBOT_HARNESS_MODE",
        "MONKEYBOT_REALTIME_WS_ENABLED",
        "MONKEYBOT_REALTIME_WS_PORT",
        "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC",
        "MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE",
    ):
        monkeypatch.delenv(key, raising=False)
    runtime_env.apply_monkeybot_runtime_env()
    assert os.environ.get("MONKEYBOT_HARNESS_MODE") == "realtime"
    assert os.environ.get("MONKEYBOT_REALTIME_WS_ENABLED") == "true"
    assert os.environ.get("MONKEYBOT_REALTIME_WS_PORT") == "9090"
    assert os.environ.get("MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC") == "900"
    assert os.environ.get("MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE") == "false"
