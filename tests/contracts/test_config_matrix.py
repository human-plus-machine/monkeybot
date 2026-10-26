"""T1-CFG-01..06. Config precedence, tiers, retired sections, validation, aliases, paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from monkeybot.core.config.runtime_env import (
    ENV_MAP,
    ENV_SPEC,
    YAML_ONLY_ENV_KEYS,
    ConfigTier,
    apply_monkeybot_runtime_env,
    reset_runtime_env_state_for_tests,
    warn_retired_sections,
)
from monkeybot.core.config.settings import (
    ConfigError,
    get_provider_config,
    normalize_model_provider,
)
from monkeybot.core.config.snapshot import get_config_store
from monkeybot.core.config.validation import validate_monkeybot_yaml_doc
from monkeybot.core.layout import resolve_memory_storage_uri, resolve_sqlite_url

_TABLE = Path(__file__).resolve().parent / "tables" / "config_precedence.yaml"


@pytest.fixture(autouse=True)
def _reset_config_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    before = {key: os.environ.get(key) for key in ENV_MAP.values()}
    for key in YAML_ONLY_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    reset_runtime_env_state_for_tests()
    for key, previous in before.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _snapshot_value(dotted: str) -> Any:
    current: Any = get_config_store().current()
    for part in dotted.split("."):
        current = getattr(current, part)
    return current


def _apply_case(root: Path, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _write_files(root, case.get("files") or {})
    for key in case.get("unset") or []:
        monkeypatch.delenv(key, raising=False)
    for key, value in (case.get("env") or {}).items():
        monkeypatch.setenv(key, str(value))
    selected = case.get("monkeybot_config")
    if isinstance(selected, str):
        monkeypatch.setenv("MONKEYBOT_CONFIG", selected)
        apply_monkeybot_runtime_env(agent_root=root)
        return
    config = case.get("config")
    config_path = root / str(config) if isinstance(config, str) else None
    apply_monkeybot_runtime_env(config_path=config_path, agent_root=root)


@pytest.mark.case("T1-CFG-01")
def test_precedence_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env beats YAML and includes; includes beat the base file; model.* is YAML-only."""
    document = yaml.safe_load(_TABLE.read_text(encoding="utf-8"))
    cases = document["cases"]
    assert cases, "config precedence table is empty"
    for case in cases:
        reset_runtime_env_state_for_tests()
        for key in YAML_ONLY_ENV_KEYS:
            os.environ.pop(key, None)
        root = tmp_path / str(case["id"])
        root.mkdir()
        monkeypatch.chdir(root)
        expect = case["expect"]
        if expect.get("raises"):
            with pytest.raises(ConfigError, match=str(expect.get("match") or "")):
                _apply_case(root, case, monkeypatch)
            continue
        _apply_case(root, case, monkeypatch)
        for key, value in (expect.get("environ") or {}).items():
            assert os.environ.get(key) == value, case["id"]
        for key in expect.get("environ_absent") or []:
            assert key not in os.environ, case["id"]
        for dotted, value in (expect.get("snapshot") or {}).items():
            assert _snapshot_value(dotted) == value, f"{case['id']} {dotted}"


@pytest.mark.case("T1-CFG-02")
def test_every_env_key_has_one_tier() -> None:
    """Every ENV_MAP key maps to exactly one ConfigTier."""
    env_names = list(ENV_MAP.values())
    assert len(env_names) == len(set(env_names))
    assert set(env_names) == set(ENV_SPEC)
    for env_name, (tier, path) in ENV_SPEC.items():
        assert isinstance(tier, ConfigTier)
        assert tier in set(ConfigTier)
        assert path
        assert env_name in ENV_MAP.values()


@pytest.mark.case("T1-CFG-03")
def test_retired_sections_warned_and_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """knowledge: and context_curation: are warned once and do not become settings."""
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\nknowledge:\n  enabled: true\ncontext_curation:\n  window: 8\n",
        encoding="utf-8",
    )
    before = set(os.environ)
    with caplog.at_level("WARNING", logger="monkeybot.core.config.runtime_env"):
        apply_monkeybot_runtime_env(agent_root=tmp_path)
        warn_retired_sections({"knowledge": {"enabled": True}, "context_curation": {"window": 8}})
    assert "knowledge is retired and ignored" in caplog.text
    assert "context_curation is retired and ignored" in caplog.text
    assert caplog.text.count("knowledge is retired and ignored") == 1
    assert caplog.text.count("context_curation is retired and ignored") == 1
    assert set(os.environ) == before
    assert get_config_store().current().model.provider == "fake"
    sections = {section for section, _key in ENV_MAP}
    assert "knowledge" not in sections
    assert "context_curation" not in sections


@pytest.mark.case("T1-CFG-04")
def test_memory_backend_s3_rejected() -> None:
    """memory.backend: s3 is rejected by YAML validation."""
    with pytest.raises(ConfigError, match="memory.backend"):
        validate_monkeybot_yaml_doc({"memory": {"backend": "s3"}, "model": {"provider": "fake"}})


@pytest.mark.case("T1-CFG-04")
@pytest.mark.parametrize("provider", ["not-a-real-provider", "azure_openai"])
def test_unknown_provider_ids_rejected(provider: str) -> None:
    """Unknown model.provider ids are rejected."""
    with pytest.raises(ConfigError, match="not supported"):
        validate_monkeybot_yaml_doc({"model": {"provider": provider}})


@pytest.mark.case("T1-CFG-04")
def test_model_provider_fake_rejected_by_get_provider_config() -> None:
    """MODEL_PROVIDER=fake is rejected by get_provider_config."""
    with pytest.raises(ValueError, match="MODEL_PROVIDER=fake"):
        get_provider_config(provider="fake")


@pytest.mark.case("T1-CFG-05")
@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("gemini", "google_vertexai"),
        ("vertex", "google_vertexai"),
        ("vertex-claude", "vertex_anthropic"),
        ("ollama_cloud", "ollama-cloud"),
    ],
)
def test_provider_aliases(alias: str, canonical: str) -> None:
    """YAML provider aliases normalize to the canonical provider id."""
    assert normalize_model_provider(alias) == canonical


@pytest.mark.case("T1-CFG-06")
def test_relative_paths_anchor_agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlite:///x.db and a memory URI resolve against the agent root, not the cwd."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for key in ("DB_URL", "MEMORY_STORAGE_URI"):
        monkeypatch.delenv(key, raising=False)
    cfg = agent_root / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n"
        "paths:\n"
        "  db_url: sqlite:///x.db\n"
        "  memory_storage_uri: local://memory\n",
        encoding="utf-8",
    )
    apply_monkeybot_runtime_env(agent_root=agent_root)
    current = get_config_store().current()
    assert current.paths.db_url == resolve_sqlite_url("sqlite:///x.db", agent_root)
    assert current.paths.memory_storage_uri == resolve_memory_storage_uri(
        "local://memory", agent_root
    )
    assert current.paths.db_url == f"sqlite:///{(agent_root / 'x.db').resolve()}"
    assert str(current.paths.memory_storage_uri).startswith("local://")
    assert str(current.paths.memory_storage_uri).endswith(str((agent_root / "memory").resolve()))
    assert elsewhere.as_posix() not in str(current.paths.db_url)
    assert elsewhere.as_posix() not in str(current.paths.memory_storage_uri)
