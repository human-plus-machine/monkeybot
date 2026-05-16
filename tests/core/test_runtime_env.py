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
    yield
    runtime_env.reset_runtime_env_state_for_tests()


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
