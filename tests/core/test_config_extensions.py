"""Tests for config extensions (subagents, vertex_anthropic, custom memory folders)."""

import os

import pytest

import monkeybot.core.config as config_mod
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
        config_mod._config_loaded = False
        load_bot_config()
        assert os.environ.get("AGENT_NAME") == "test"
        config_mod._config_loaded = False


class TestVertexAnthropicProvider:
    """Tests for vertex_anthropic provider support."""

    def test_get_model_vertex_anthropic_happy_path(self, monkeypatch):
        """Test successful initialization of ChatAnthropicVertex with all required env vars."""
        from unittest.mock import MagicMock, patch

        from monkeybot.core.config import get_model

        # Set up environment
        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("VERTEX_AI_LOCATION", "us-central1")

        # Mock the ChatAnthropicVertex class
        mock_chat_anthropic = MagicMock()
        with patch(
            "langchain_google_vertexai.model_garden.ChatAnthropicVertex",
            return_value=mock_chat_anthropic,
        ) as mock_class:
            model = get_model(
                provider="vertex_anthropic",
                model_name="claude-3-5-sonnet@20240620",
                temperature=0.5,
                max_tokens=4096,
            )

            # Verify the model was created
            assert model == mock_chat_anthropic

            # Verify constructor was called with correct args
            mock_class.assert_called_once_with(
                model_name="claude-3-5-sonnet@20240620",
                project="test-project",
                location="us-central1",
                temperature=0.5,
                max_tokens=4096,
            )

    def test_get_model_vertex_anthropic_missing_project(self, monkeypatch):
        """Test that ValueError is raised when GCP_PROJECT_ID is not set."""
        from monkeybot.core.config import get_model

        # Clear any project env vars
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        monkeypatch.delenv("VERTEX_AI_PROJECT_ID", raising=False)

        # Mock the import to succeed
        from unittest.mock import MagicMock, patch

        with patch("langchain_google_vertexai.model_garden.ChatAnthropicVertex", MagicMock()):
            try:
                get_model(provider="vertex_anthropic", model_name="claude-3-5-sonnet@20240620")
                assert False, "Expected ValueError to be raised"
            except ValueError as e:
                assert "vertex_anthropic provider requires GCP_PROJECT_ID" in str(e)
                assert "Set gcp.project_id in bot.yaml" in str(e)

    def test_get_model_vertex_anthropic_missing_import(self, monkeypatch):
        """Test that ImportError is raised when anthropic[vertex] is not installed."""
        from monkeybot.core.config import get_model

        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")

        # Mock the import to fail
        from unittest.mock import patch

        with patch.dict("sys.modules", {"langchain_google_vertexai.model_garden": None}):
            with patch(
                "builtins.__import__",
                side_effect=ImportError("No module named 'langchain_google_vertexai'"),
            ):
                try:
                    get_model(provider="vertex_anthropic", model_name="claude-3-5-sonnet@20240620")
                    assert False, "Expected ImportError to be raised"
                except ImportError as e:
                    assert "anthropic[vertex] is required" in str(e)
                    assert "pip install 'anthropic[vertex]'" in str(e)

    def test_get_model_vertex_anthropic_default_location(self, monkeypatch):
        """Test that us-east5 is used as default location when VERTEX_AI_LOCATION is not set."""
        from unittest.mock import MagicMock, patch

        from monkeybot.core.config import get_model

        # Set up environment with project but no location
        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
        monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)

        # Mock the ChatAnthropicVertex class
        mock_chat_anthropic = MagicMock()
        with patch(
            "langchain_google_vertexai.model_garden.ChatAnthropicVertex",
            return_value=mock_chat_anthropic,
        ) as mock_class:
            get_model(
                provider="vertex_anthropic",
                model_name="claude-3-5-sonnet@20240620",
                temperature=0.7,
                max_tokens=8192,
            )

            # Verify default location was used
            mock_class.assert_called_once_with(
                model_name="claude-3-5-sonnet@20240620",
                project="test-project",
                location="us-east5",
                temperature=0.7,
                max_tokens=8192,
            )

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
        config_mod._raw_yaml = None
        config_mod._config_loaded = False

    def teardown_method(self):
        config_mod._raw_yaml = None
        config_mod._config_loaded = False

    def test_returns_empty_list_when_raw_yaml_is_none(self):
        config_mod._raw_yaml = None
        assert get_subagent_configs() == []

    def test_returns_empty_list_when_no_subagents_key(self):
        config_mod._raw_yaml = {"agent": {"name": "test"}}
        assert get_subagent_configs() == []

    def test_returns_empty_list_when_subagents_is_empty_list(self):
        config_mod._raw_yaml = {"subagents": []}
        assert get_subagent_configs() == []

    def test_parses_single_subagent(self):
        config_mod._raw_yaml = {
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
        config_mod._raw_yaml = {
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
        config_mod._raw_yaml = {
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
        config_mod._raw_yaml = {
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
        config_mod._raw_yaml = {
            "subagents": [
                {"description": "No name field.", "skills": []},
                {"name": "valid", "description": "Valid entry.", "skills": []},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "valid"

    def test_skips_entry_missing_description(self):
        config_mod._raw_yaml = {
            "subagents": [
                {"name": "no-desc", "skills": []},
                {"name": "valid", "description": "Valid.", "skills": []},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "valid"

    def test_skips_non_dict_entry(self):
        config_mod._raw_yaml = {
            "subagents": [
                "not-a-dict",
                {"name": "valid", "description": "Valid.", "skills": []},
            ]
        }
        configs = get_subagent_configs()
        assert len(configs) == 1

    def test_skills_defaults_to_empty_list_when_absent(self):
        config_mod._raw_yaml = {
            "subagents": [{"name": "sa", "description": "desc."}]
        }
        cfg = get_subagent_configs()[0]
        assert cfg.skills == []

    def test_parses_vertex_location_alias(self):
        config_mod._raw_yaml = {
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
        config_mod._raw_yaml = {
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
        config_mod._raw_yaml = {
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
        config_mod._config_loaded = False
        config_mod._raw_yaml = None

        load_bot_config()
        configs = get_subagent_configs()

        assert len(configs) == 1
        assert configs[0].name == "content-intel"
        assert configs[0].skills == ["./skills/content-intelligence/"]

        config_mod._config_loaded = False
        config_mod._raw_yaml = None
