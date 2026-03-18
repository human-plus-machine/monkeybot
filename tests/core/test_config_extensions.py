"""Tests for HeartbeatConfig, VoiceConfig dataclasses and loader functions."""
import pytest

from src.core.config import (
    CONFIG_MAPPING,
    ConfigError,
    CustomMemoryFolder,
    HeartbeatConfig,
    VoiceConfig,
    load_bot_config,
    load_heartbeat_config,
    load_voice_config,
)


class TestHeartbeatConfig:
    def test_default_values(self):
        cfg = HeartbeatConfig()
        assert cfg.cron == "*/30 * * * *"
        assert cfg.active_hours_start == "09:00"
        assert cfg.active_hours_timezone == "America/New_York"
        assert cfg.heartbeat_md_path is None

    def test_load_heartbeat_config_disabled(self, monkeypatch):
        monkeypatch.delenv("HEARTBEAT_ENABLED", raising=False)
        assert load_heartbeat_config() is None

    def test_load_heartbeat_config_false(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "false")
        assert load_heartbeat_config() is None

    def test_load_heartbeat_config_enabled(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("HEARTBEAT_CRON", "*/15 * * * *")
        monkeypatch.setenv("HEARTBEAT_ACTIVE_HOURS_START", "08:00")
        cfg = load_heartbeat_config()
        assert cfg is not None
        assert isinstance(cfg, HeartbeatConfig)
        assert cfg.cron == "*/15 * * * *"
        assert cfg.active_hours_start == "08:00"

    def test_load_heartbeat_config_defaults_when_enabled(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        for key in ["HEARTBEAT_CRON", "HEARTBEAT_ACTIVE_HOURS_START", "HEARTBEAT_ACTIVE_HOURS_END"]:
            monkeypatch.delenv(key, raising=False)
        cfg = load_heartbeat_config()
        assert cfg.cron == "*/30 * * * *"


class TestVoiceConfig:
    def test_default_values(self):
        cfg = VoiceConfig()
        assert cfg.language_code == "en-US"
        assert cfg.tts_voice_name == "en-US-Journey-F"
        assert cfg.tts_audio_encoding == "OGG_OPUS"

    def test_load_voice_config_disabled(self, monkeypatch):
        monkeypatch.delenv("VOICE_ENABLED", raising=False)
        assert load_voice_config() is None

    def test_load_voice_config_enabled(self, monkeypatch):
        monkeypatch.setenv("VOICE_ENABLED", "true")
        monkeypatch.setenv("VOICE_TTS_VOICE_NAME", "en-US-Journey-D")
        cfg = load_voice_config()
        assert cfg is not None
        assert isinstance(cfg, VoiceConfig)
        assert cfg.tts_voice_name == "en-US-Journey-D"

    def test_load_voice_config_defaults_when_enabled(self, monkeypatch):
        monkeypatch.setenv("VOICE_ENABLED", "true")
        for key in ["VOICE_STT_LANGUAGE_CODE", "VOICE_TTS_VOICE_NAME"]:
            monkeypatch.delenv(key, raising=False)
        cfg = load_voice_config()
        assert cfg.language_code == "en-US"
        assert cfg.tts_voice_name == "en-US-Journey-F"


class TestConfigMappingExtensions:
    def test_heartbeat_keys_in_mapping(self):
        assert "heartbeat.enabled" in CONFIG_MAPPING
        assert CONFIG_MAPPING["heartbeat.enabled"] == "HEARTBEAT_ENABLED"
        assert "heartbeat.active_hours.start" in CONFIG_MAPPING
        assert CONFIG_MAPPING["heartbeat.active_hours.start"] == "HEARTBEAT_ACTIVE_HOURS_START"

    def test_voice_keys_in_mapping(self):
        assert "voice.enabled" in CONFIG_MAPPING
        assert CONFIG_MAPPING["voice.enabled"] == "VOICE_ENABLED"
        assert "voice.text_to_speech.voice_name" in CONFIG_MAPPING

    def test_load_bot_config_with_heartbeat_yaml(self, tmp_path, monkeypatch):
        import os

        import src.core.config as cfg_mod
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text(
            "agent:\n  name: test\n"
            "heartbeat:\n  enabled: true\n  cron: '*/30 * * * *'\n"
        )
        monkeypatch.chdir(tmp_path)
        cfg_mod._config_loaded = False
        monkeypatch.delenv("HEARTBEAT_ENABLED", raising=False)
        load_bot_config()
        assert os.environ.get("HEARTBEAT_ENABLED").lower() == "true"
        assert os.environ.get("HEARTBEAT_CRON") == "*/30 * * * *"
        cfg_mod._config_loaded = False

    def test_bot_yaml_without_heartbeat_no_error(self, tmp_path, monkeypatch):
        import src.core.config as cfg_mod
        bot_yaml = tmp_path / "bot.yaml"
        bot_yaml.write_text("agent:\n  name: test\nmodel:\n  provider: google_vertexai\n")
        monkeypatch.chdir(tmp_path)
        cfg_mod._config_loaded = False
        load_bot_config()
        cfg_mod._config_loaded = False


class TestVertexAnthropicProvider:
    """Tests for vertex_anthropic provider support."""

    def test_get_model_vertex_anthropic_happy_path(self, monkeypatch):
        """Test successful initialization of ChatAnthropicVertex with all required env vars."""
        from unittest.mock import MagicMock, patch

        from src.core.config import get_model

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
        from src.core.config import get_model

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
        from src.core.config import get_model

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

        from src.core.config import get_model

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
        from src.core.config import _validate_provider_config

        config = {
            "MODEL_PROVIDER": "vertex_anthropic",
            "GCP_PROJECT_ID": "test-project",
            "MEMORY_BACKEND": "local",
            "SCHEDULER_STORAGE": "json",
            "SECRETS_PROVIDER": "env",
        }

        # Should not raise any exception
        _validate_provider_config(config)

    def test_validate_provider_config_vertex_anthropic_no_project(self, monkeypatch):
        """Test that ConfigError is raised when vertex_anthropic is used without GCP_PROJECT_ID."""
        from src.core.config import ConfigError, _validate_provider_config

        config = {
            "MODEL_PROVIDER": "vertex_anthropic",
            "MEMORY_BACKEND": "local",
            "SCHEDULER_STORAGE": "json",
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


class TestHeartbeatConfigNewFields:
    def test_default_values(self):
        cfg = HeartbeatConfig()
        assert cfg.notify_on_complete is True
        assert cfg.notify_report_format == "standup"
        assert cfg.council_enabled is False
        assert cfg.council_model == "gemini-2.0-flash"
        assert cfg.council_custom_folders is None
        assert cfg.identity_md_path is None


class TestLoadHeartbeatConfigNewEnvVars:
    def test_notify_on_complete_false(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("HEARTBEAT_NOTIFY_ON_COMPLETE", "false")
        cfg = load_heartbeat_config()
        assert cfg.notify_on_complete is False

    def test_notify_format_minimal(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("HEARTBEAT_NOTIFY_FORMAT", "minimal")
        cfg = load_heartbeat_config()
        assert cfg.notify_report_format == "minimal"

    def test_invalid_notify_format_defaults_to_standup(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("HEARTBEAT_NOTIFY_FORMAT", "xyz")
        cfg = load_heartbeat_config()
        assert cfg.notify_report_format == "standup"

    def test_council_enabled(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("HEARTBEAT_COUNCIL_ENABLED", "true")
        cfg = load_heartbeat_config()
        assert cfg.council_enabled is True

    def test_council_model_custom(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("HEARTBEAT_COUNCIL_MODEL", "gemini-2.0-flash-lite")
        cfg = load_heartbeat_config()
        assert cfg.council_model == "gemini-2.0-flash-lite"


class TestLoadHeartbeatConfigCustomFolders:
    def test_parses_custom_folders_from_yaml(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        raw = {"custom_folders": [{"name": "campaigns", "description": "Campaign data"}]}
        cfg = load_heartbeat_config(raw_yaml=raw)
        assert cfg.council_custom_folders is not None
        assert len(cfg.council_custom_folders) == 1
        assert cfg.council_custom_folders[0].name == "campaigns"

    def test_invalid_folder_entry_skipped(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        raw = {
            "custom_folders": [
                {"name": "campaigns", "description": "Good"},
                {"name": "episodic", "description": "Reserved - should skip"},
            ]
        }
        cfg = load_heartbeat_config(raw_yaml=raw)
        assert len(cfg.council_custom_folders) == 1
        assert cfg.council_custom_folders[0].name == "campaigns"

    def test_no_raw_yaml_returns_none_folders(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_ENABLED", "true")
        cfg = load_heartbeat_config(raw_yaml=None)
        assert cfg.council_custom_folders is None
