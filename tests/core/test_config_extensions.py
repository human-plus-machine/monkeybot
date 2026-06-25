"""Tests for config extensions (subagents, vertex_anthropic, custom memory folders)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monkeybot.core.config import (
    ConfigError,
    CustomMemoryFolder,
    SubagentConfig,
    apply_monkeybot_runtime_env,
    cache_enabled_from_env,
    get_provider_config,
    get_subagent_configs,
    get_subagent_registry,
    normalize_model_provider,
    reset_runtime_env_state_for_tests,
    validate_monkeybot_yaml_doc,
    validate_provider_env,
)
from monkeybot.core.config.runtime_env import ENV_MAP


class TestEnvMap:
    def test_model_provider_maps(self) -> None:
        assert ENV_MAP[("model", "provider")] == "MODEL_PROVIDER"

    def test_sandbox_keys_in_env_map(self) -> None:
        assert ENV_MAP[("sandbox", "enabled")] == "SANDBOX_ENABLED"
        assert ENV_MAP[("sandbox", "server_url")] == "SANDBOX_SERVER_URL"
        assert ENV_MAP[("sandbox", "image")] == "SANDBOX_IMAGE"


class TestVertexAnthropicProvider:
    def test_normalize_model_provider_vertex_alias(self) -> None:
        assert normalize_model_provider("vertex") == "google_vertexai"
        assert normalize_model_provider("vertex-claude") == "vertex_anthropic"

    def test_get_provider_config_huggingface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_TOKEN", "hf_test")
        cfg = get_provider_config(provider="huggingface", model_name="meta-llama/Llama-3.1-8B-Instruct")
        assert cfg.provider.name == "huggingface"
        assert cfg.model == "meta-llama/Llama-3.1-8B-Instruct"

    def test_get_provider_config_vertex_anthropic_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("ANTHROPIC_VERTEX_REGION", "us-central1")
        mock_instance = MagicMock()
        with patch(
            "monkeybot.core.config.settings.VertexClaudeProvider",
            return_value=mock_instance,
        ) as mock_cls:
            cfg = get_provider_config(
                provider="vertex_anthropic",
                model_name="claude-3-5-sonnet@20240620",
            )
            assert cfg.provider is mock_instance
            assert cfg.model == "claude-3-5-sonnet@20240620"
            mock_cls.assert_called_once_with(
                project_id="test-project",
                region="us-central1",
                temperature=0.7,
                max_tokens=60_000,
                cache_enabled=True,
            )

    def test_get_provider_config_vertex_anthropic_missing_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "GCP_PROJECT_ID",
            "VERTEX_AI_PROJECT_ID",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "GOOGLE_CLOUD_PROJECT",
        ):
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(ValueError, match="vertex_anthropic provider requires"):
            get_provider_config(provider="vertex_anthropic", model_name="claude-3-5-sonnet@20240620")

    def test_validate_provider_env_vertex_anthropic_accepted(self) -> None:
        validate_provider_env(
            {
                "MODEL_PROVIDER": "vertex_anthropic",
                "GCP_PROJECT_ID": "test-project",
                "MEMORY_BACKEND": "local",
                "SECRETS_PROVIDER": "env",
            }
        )

    def test_validate_provider_env_vertex_anthropic_no_project(self) -> None:
        with pytest.raises(ConfigError, match="gcp.project_id is not configured"):
            validate_provider_env(
                {
                    "MODEL_PROVIDER": "vertex_anthropic",
                    "MEMORY_BACKEND": "local",
                    "SECRETS_PROVIDER": "env",
                }
            )


class TestCustomMemoryFolder:
    def test_valid_name_and_description(self) -> None:
        folder = CustomMemoryFolder("campaigns", "Marketing campaign data")
        assert folder.name == "campaigns"

    def test_invalid_name_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="invalid"):
            CustomMemoryFolder("My Folder", "desc")

    def test_reserved_name_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="conflicts"):
            CustomMemoryFolder("episodic", "desc")


class TestSubagentConfig:
    def test_required_fields(self) -> None:
        cfg = SubagentConfig(
            name="content-intel",
            description="Research top-performing content.",
            skills=["./skills/content-intelligence/"],
        )
        assert cfg.name == "content-intel"
        assert cfg.skills == ["./skills/content-intelligence/"]


class TestGetSubagentConfigs:
    def _write_config(self, tmp_path: Path, yaml_text: str) -> Path:
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir(parents=True)
        path = cfg_dir / "monkeybot.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        return path

    def test_returns_empty_when_no_subagents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(tmp_path, "model:\n  provider: gemini\n  name: test\n")
        assert get_subagent_configs() == []

    def test_parses_single_subagent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "model:\n  provider: gemini\n  name: test\n"
            "subagents:\n"
            "  - name: content-intel\n"
            "    description: Research content.\n"
            "    skills:\n"
            "      - ./skills/content-intelligence/\n",
        )
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "content-intel"
        assert configs[0].skills == ["./skills/content-intelligence/"]

    def test_skips_entry_missing_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  - description: No name.\n"
            "    skills: []\n"
            "  - name: valid\n"
            "    description: Valid.\n"
            "    skills: []\n",
        )
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "valid"


class TestGetSubagentRegistry:
    def _write_config(self, tmp_path: Path, yaml_text: str) -> None:
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "monkeybot.yaml").write_text(yaml_text, encoding="utf-8")

    def test_registry_maps_by_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  - name: alpha\n"
            "    description: First.\n"
            "    agent_md: ./agents/alpha.md\n"
            "  - name: beta\n"
            "    description: Second.\n"
            "    agent_md: ./agents/beta.md\n",
        )
        reg = get_subagent_registry()
        assert set(reg) == {"alpha", "beta"}
        assert reg["alpha"].agent_md == "./agents/alpha.md"

    def test_duplicate_name_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  - name: dup\n"
            "    description: One.\n"
            "  - name: dup\n"
            "    description: Two.\n",
        )
        with pytest.raises(ConfigError, match="Duplicate subagent name"):
            get_subagent_registry()

    def test_prompt_file_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  - name: legacy\n"
            "    description: Legacy alias.\n"
            "    prompt_file: ./agents/legacy.md\n",
        )
        reg = get_subagent_registry()
        assert reg["legacy"].agent_md == "./agents/legacy.md"


class TestCacheEnabled:
    def test_cache_enabled_from_env_default_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODEL_ENABLE_CACHING", raising=False)
        assert cache_enabled_from_env() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "FALSE", " off "])
    def test_cache_enabled_from_env_falsey_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("MODEL_ENABLE_CACHING", value)
        assert cache_enabled_from_env() is False

    def test_enable_caching_from_monkeybot_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "monkeybot.yaml").write_text(
            "model:\n  provider: gemini\n  name: test\n  enable_caching: false\n",
            encoding="utf-8",
        )
        env_before = os.environ.get("MODEL_ENABLE_CACHING")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MODEL_ENABLE_CACHING", raising=False)
        reset_runtime_env_state_for_tests()
        try:
            apply_monkeybot_runtime_env()
            assert os.environ["MODEL_ENABLE_CACHING"] == "false"
            assert cache_enabled_from_env() is False
        finally:
            reset_runtime_env_state_for_tests()
            if env_before is None:
                os.environ.pop("MODEL_ENABLE_CACHING", None)
            else:
                os.environ["MODEL_ENABLE_CACHING"] = env_before

    def test_sandbox_from_monkeybot_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "monkeybot.yaml").write_text(
            "model:\n  provider: gemini\n  name: test\n"
            "sandbox:\n  enabled: true\n  server_url: http://myserver:9090\n  image: python:3.11\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        for key in ("SANDBOX_ENABLED", "SANDBOX_SERVER_URL", "SANDBOX_IMAGE"):
            monkeypatch.delenv(key, raising=False)
        reset_runtime_env_state_for_tests()
        apply_monkeybot_runtime_env()
        assert os.environ.get("SANDBOX_ENABLED", "").lower() == "true"
        assert os.environ.get("SANDBOX_SERVER_URL") == "http://myserver:9090"
        assert os.environ.get("SANDBOX_IMAGE") == "python:3.11"
        reset_runtime_env_state_for_tests()


class TestValidateMonkeybotYamlDoc:
    def test_rejects_unknown_provider(self) -> None:
        with pytest.raises(ConfigError, match="not supported"):
            validate_monkeybot_yaml_doc({"model": {"provider": "azure_openai", "name": "x"}})

    def test_accepts_gemini(self) -> None:
        validate_monkeybot_yaml_doc({"model": {"provider": "gemini", "name": "gemini-3-flash"}})
