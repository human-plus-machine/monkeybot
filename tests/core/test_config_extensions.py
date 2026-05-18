"""Tests for config extensions (subagents, vertex_anthropic, custom memory folders)."""

import os

import pytest

import monkeybot.core.config.settings as config_state
from monkeybot.core.config import (
    CONFIG_MAPPING,
    ConfigError,
    CustomMemoryFolder,
    SubagentConfig,
    get_subagent_configs,
    load_bot_config,
)


class TestConfigMappingBasics:
    def test_agent_name_maps_to_env(self) -> None:
        assert CONFIG_MAPPING["agent.name"] == "AGENT_NAME"

    def test_load_bot_config_minimal_yaml(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text("agent:\n  name: test\nmodel:\n  provider: google_vertexai\n")
        monkeypatch.chdir(tmp_path)
        # bot.yaml only applies when env does not already set the key; clear leaked shell env.
        monkeypatch.delenv("MODEL_PROVIDER", raising=False)
        config_state._config_loaded = False
        load_bot_config()
        assert os.environ.get("AGENT_NAME") == "test"
        config_state._config_loaded = False


class TestVertexAnthropicProvider:
    """Tests for vertex_anthropic native VertexClaudeProvider wiring."""

    def test_normalize_model_provider_vertex_alias(self) -> None:
        from monkeybot.core.config import normalize_model_provider

        assert normalize_model_provider("vertex") == "google_vertexai"
        assert normalize_model_provider("vertex-claude") == "vertex_anthropic"

    def test_get_provider_config_huggingface(self, monkeypatch):
        from monkeybot.core.config import get_provider_config

        monkeypatch.setenv("HF_TOKEN", "hf_test")
        cfg = get_provider_config(provider="huggingface", model_name="meta-llama/Llama-3.1-8B-Instruct")
        assert cfg.provider.name == "huggingface"
        assert cfg.model == "meta-llama/Llama-3.1-8B-Instruct"

    def test_get_provider_config_vertex_anthropic_happy_path(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from monkeybot.core.config import get_provider_config

        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("VERTEX_AI_LOCATION", "us-central1")

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
            mock_cls.assert_called_once_with(project_id="test-project", region="us-central1")

    def test_get_provider_config_vertex_anthropic_missing_project(self, monkeypatch):
        from monkeybot.core.config import get_provider_config

        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        monkeypatch.delenv("VERTEX_AI_PROJECT_ID", raising=False)

        with pytest.raises(ValueError, match="vertex_anthropic provider requires"):
            get_provider_config(
                provider="vertex_anthropic",
                model_name="claude-3-5-sonnet@20240620",
            )

    def test_get_provider_config_vertex_anthropic_default_location(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from monkeybot.core.config import get_provider_config

        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_REGION", raising=False)

        mock_instance = MagicMock()
        with patch(
            "monkeybot.core.config.settings.VertexClaudeProvider",
            return_value=mock_instance,
        ) as mock_cls:
            get_provider_config(
                provider="vertex_anthropic",
                model_name="claude-3-5-sonnet@20240620",
            )

            mock_cls.assert_called_once_with(project_id="test-project", region="us-east5")

    def test_validate_provider_config_vertex_anthropic_accepted(self, monkeypatch):
        """Test that vertex_anthropic is accepted as a valid provider."""
        from monkeybot.core.config import _validate_provider_config

        config = {
            "MODEL_PROVIDER": "vertex_anthropic",
            "GCP_PROJECT_ID": "test-project",
            "MEMORY_BACKEND": "local",
            "SECRETS_PROVIDER": "env",
        }

        # Should not raise any exception
        _validate_provider_config(config)

    def test_validate_provider_config_vertex_anthropic_no_project(self, monkeypatch):
        """Test that ConfigError is raised when vertex_anthropic is used without GCP_PROJECT_ID."""
        from monkeybot.core.config import ConfigError, _validate_provider_config

        config = {
            "MODEL_PROVIDER": "vertex_anthropic",
            "MEMORY_BACKEND": "local",
            "SECRETS_PROVIDER": "env",
        }

        try:
            _validate_provider_config(config)
            assert False, "Expected ConfigError to be raised"
        except ConfigError as e:
            assert "model.provider is set to 'vertex_anthropic'" in str(e)
            assert "gcp.project_id is not configured" in str(e)
            assert "Add 'gcp.project_id: your-project-id' to bot.yaml" in str(e)


class TestCustomMemoryFolder:
    def test_valid_name_and_description(self):
        folder = CustomMemoryFolder("campaigns", "Marketing campaign data")
        assert folder.name == "campaigns"
        assert folder.description == "Marketing campaign data"

    def test_invalid_name_raises_config_error(self):
        with pytest.raises(ConfigError, match="invalid"):
            CustomMemoryFolder("My Folder", "desc")

    def test_reserved_name_raises_config_error(self):
        with pytest.raises(ConfigError, match="conflicts"):
            CustomMemoryFolder("episodic", "desc")

    def test_name_with_hyphens_is_valid(self):
        folder = CustomMemoryFolder("sprint-goals", "Sprint planning")
        assert folder.name == "sprint-goals"

    def test_name_with_numbers_is_valid(self):
        folder = CustomMemoryFolder("q1-2026", "Q1 results")
        assert folder.name == "q1-2026"


# ---------------------------------------------------------------------------
# SubagentConfig dataclass
# ---------------------------------------------------------------------------

class TestSubagentConfig:
    def test_required_fields(self):
        cfg = SubagentConfig(
            name="content-intel",
            description="Research top-performing content.",
            skills=["./skills/content-intelligence/"],
        )
        assert cfg.name == "content-intel"
        assert cfg.description == "Research top-performing content."
        assert cfg.skills == ["./skills/content-intelligence/"]

    def test_optional_fields_default_to_none(self):
        cfg = SubagentConfig(
            name="analytics",
            description="Query GA4.",
            skills=[],
        )
        assert cfg.prompt_file is None
        assert cfg.model is None
        assert cfg.vertex_location is None

    def test_optional_fields_can_be_set(self):
        cfg = SubagentConfig(
            name="analytics",
            description="Query GA4.",
            skills=["./skills/analytics/"],
            prompt_file="./prompts/analytics.md",
            model="gemini-2.0-flash",
        )
        assert cfg.prompt_file == "./prompts/analytics.md"
        assert cfg.model == "gemini-2.0-flash"

    def test_multiple_skills_dirs(self):
        cfg = SubagentConfig(
            name="writer",
            description="Generate content.",
            skills=["./skills/base/", "./skills/content-creation/"],
        )
        assert len(cfg.skills) == 2


# ---------------------------------------------------------------------------
# get_subagent_configs()
# ---------------------------------------------------------------------------

class TestGetSubagentConfigs:
    def setup_method(self):
        """Reset module-level state before each test."""
        config_state._raw_yaml = None
        config_state._config_loaded = False

    def teardown_method(self):
        config_state._raw_yaml = None
        config_state._config_loaded = False

    def test_returns_empty_list_when_raw_yaml_is_none(self):
        config_state._raw_yaml = None
        assert get_subagent_configs() == []

    def test_returns_empty_list_when_no_subagents_key(self):
        config_state._raw_yaml = {"agent": {"name": "test"}}
        assert get_subagent_configs() == []

    def test_returns_empty_list_when_subagents_is_empty_list(self):
        config_state._raw_yaml = {"subagents": []}
        assert get_subagent_configs() == []

    def test_parses_single_subagent(self):
        config_state._raw_yaml = {
            "subagents": [
                {
                    "name": "content-intel",
                    "description": "Research content.",
                    "skills": ["./skills/content-intelligence/"],
                }
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "content-intel"
        assert configs[0].description == "Research content."
        assert configs[0].skills == ["./skills/content-intelligence/"]
        assert configs[0].prompt_file is None
        assert configs[0].model is None
        assert configs[0].vertex_location is None

    def test_parses_multiple_subagents(self):
        config_state._raw_yaml = {
            "subagents": [
                {"name": "agent-a", "description": "A.", "skills": []},
                {"name": "agent-b", "description": "B.", "skills": ["./skills/b/"]},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 2
        assert configs[0].name == "agent-a"
        assert configs[1].name == "agent-b"

    def test_with_prompt_file_and_model(self):
        config_state._raw_yaml = {
            "subagents": [
                {
                    "name": "analytics",
                    "description": "Query GA4.",
                    "skills": ["./skills/analytics/"],
                    "prompt_file": "./prompts/analytics.md",
                    "model": "gemini-2.0-flash",
                }
            ]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.prompt_file == "./prompts/analytics.md"
        assert cfg.model == "gemini-2.0-flash"

    def test_with_multiple_skills_dirs(self):
        config_state._raw_yaml = {
            "subagents": [
                {
                    "name": "writer",
                    "description": "Write content.",
                    "skills": ["./skills/base/", "./skills/content-creation/"],
                }
            ]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.skills == ["./skills/base/", "./skills/content-creation/"]

    def test_skips_entry_missing_name(self):
        config_state._raw_yaml = {
            "subagents": [
                {"description": "No name field.", "skills": []},
                {"name": "valid", "description": "Valid entry.", "skills": []},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "valid"

    def test_skips_entry_missing_description(self):
        config_state._raw_yaml = {
            "subagents": [
                {"name": "no-desc", "skills": []},
                {"name": "valid", "description": "Valid.", "skills": []},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "valid"

    def test_skips_non_dict_entry(self):
        config_state._raw_yaml = {
            "subagents": [
                "not-a-dict",
                {"name": "valid", "description": "Valid.", "skills": []},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1

    def test_skills_defaults_to_empty_list_when_absent(self):
        config_state._raw_yaml = {
            "subagents": [{"name": "sa", "description": "desc."}]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.skills == []

    def test_parses_vertex_location_alias(self):
        config_state._raw_yaml = {
            "subagents": [
                {
                    "name": "analytics",
                    "description": "Query GA4.",
                    "skills": ["./skills/analytics/"],
                    "vertex_location": "us-east5",
                }
            ]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.vertex_location == "us-east5"

    def test_parses_location_shorthand(self):
        config_state._raw_yaml = {
            "subagents": [
                {
                    "name": "writer",
                    "description": "Write.",
                    "skills": [],
                    "location": "us-central1",
                }
            ]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.vertex_location == "us-central1"

    def test_vertex_location_wins_over_location_when_both_set(self):
        config_state._raw_yaml = {
            "subagents": [
                {
                    "name": "sa",
                    "description": "d",
                    "skills": [],
                    "location": "us-central1",
                    "vertex_location": "us-east5",
                }
            ]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.vertex_location == "us-east5"

    def test_bot_yaml_with_subagents_section(self, tmp_path, monkeypatch):
        """End-to-end: write bot.yaml, load config, verify get_subagent_configs()."""
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text(
            "agent:\n  name: test\n"
            "subagents:\n"
            "  - name: content-intel\n"
            "    description: Research content.\n"
            "    skills:\n"
            "      - ./skills/content-intelligence/\n"
        )
        monkeypatch.chdir(tmp_path)
        config_state._config_loaded = False
        config_state._raw_yaml = None

        load_bot_config()
        configs = get_subagent_configs()

        assert len(configs) == 1
        assert configs[0].name == "content-intel"
        assert configs[0].skills == ["./skills/content-intelligence/"]

        config_state._config_loaded = False
        config_state._raw_yaml = None

class TestSandboxConfigMapping:
    """Verify sandbox.* keys are wired in CONFIG_MAPPING and DEFAULTS."""

    def test_sandbox_keys_in_config_mapping(self):
        assert CONFIG_MAPPING["sandbox.enabled"] == "SANDBOX_ENABLED"
        assert CONFIG_MAPPING["sandbox.server_url"] == "SANDBOX_SERVER_URL"
        assert CONFIG_MAPPING["sandbox.image"] == "SANDBOX_IMAGE"

    def test_sandbox_defaults_are_safe(self):
        from monkeybot.core.config import DEFAULTS

        assert DEFAULTS["SANDBOX_ENABLED"] == "false"
        assert DEFAULTS["SANDBOX_SERVER_URL"] == "http://localhost:8080"
        assert DEFAULTS["SANDBOX_IMAGE"] == "python:3.12"

    def test_bot_yaml_sandbox_section_sets_env_vars(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text(
            "agent:\n  name: testbot\n"
            "model:\n  provider: google_vertexai\n"
            "sandbox:\n"
            "  enabled: true\n"
            "  server_url: http://myserver:9090\n"
            "  image: python:3.11\n"
        )
        monkeypatch.chdir(tmp_path)
        for key in ("SANDBOX_ENABLED", "SANDBOX_SERVER_URL", "SANDBOX_IMAGE"):
            monkeypatch.delenv(key, raising=False)
        config_state._config_loaded = False

        load_bot_config()

        # YAML `true` becomes Python `True` → str() → "True"; SandboxConfig.from_env()
        # handles this via .lower() == "true", so either casing is correct.
        assert os.environ.get("SANDBOX_ENABLED", "").lower() == "true"
        assert os.environ.get("SANDBOX_SERVER_URL") == "http://myserver:9090"
        assert os.environ.get("SANDBOX_IMAGE") == "python:3.11"

        config_state._config_loaded = False

    def test_bot_yaml_without_sandbox_section_uses_defaults(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text(
            "agent:\n  name: testbot\n"
            "model:\n  provider: google_vertexai\n"
        )
        monkeypatch.chdir(tmp_path)
        for key in ("SANDBOX_ENABLED", "SANDBOX_SERVER_URL", "SANDBOX_IMAGE"):
            monkeypatch.delenv(key, raising=False)
        config_state._config_loaded = False

        load_bot_config()

        assert os.environ.get("SANDBOX_ENABLED") == "false"
        assert os.environ.get("SANDBOX_SERVER_URL") == "http://localhost:8080"

        config_state._config_loaded = False

    def test_existing_env_var_not_overridden_by_bot_yaml(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        # Deployment env vars (e.g. set in Cloud Run) must win over bot.yaml.
        # If this regresses, a production override would be silently ignored.
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text(
            "agent:\n  name: testbot\n"
            "model:\n  provider: google_vertexai\n"
            "sandbox:\n  enabled: true\n  server_url: http://from-yaml:8080\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SANDBOX_ENABLED", "false")
        monkeypatch.setenv("SANDBOX_SERVER_URL", "http://from-env:9999")
        config_state._config_loaded = False

        load_bot_config()

        assert os.environ.get("SANDBOX_ENABLED") == "false"
        assert os.environ.get("SANDBOX_SERVER_URL") == "http://from-env:9999"

        config_state._config_loaded = False
