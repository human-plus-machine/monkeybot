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
    auto_schema_enabled_from_config,
    get_provider_config,
    get_realtime_config,
    get_subagent_configs,
    get_subagent_registry,
    get_subagent_settings,
    normalize_model_provider,
    reset_runtime_env_state_for_tests,
    subagent_vertex_google_search_from_config,
    validate_monkeybot_yaml_doc,
    validate_provider_env,
    vertex_google_search_enabled_from_config,
)
from monkeybot.core.config.runtime_env import ENV_MAP, RETIRED_TOOLS_KEYS, warn_retired_tools_keys
from monkeybot.core.tools.workspace_service import AGENT_READ_DEFAULT_LINES


class TestEnvMap:
    def test_model_provider_maps(self) -> None:
        assert ENV_MAP[("model", "provider")] == "MODEL_PROVIDER"

    def test_sandbox_keys_in_env_map(self) -> None:
        assert ENV_MAP[("sandbox", "enabled")] == "SANDBOX_ENABLED"
        assert ENV_MAP[("sandbox", "server_url")] == "SANDBOX_SERVER_URL"
        assert ENV_MAP[("sandbox", "image")] == "SANDBOX_IMAGE"

    def test_scheduler_enabled_in_env_map(self) -> None:
        assert ENV_MAP[("scheduler", "enabled")] == "MONKEYBOT_SCHEDULER_ENABLED"

    def test_vertex_google_search_not_in_env_map(self) -> None:
        """Config-file only (like paths.auto_schema) — no env var override."""
        assert ("web_search", "vertex_google_search") not in ENV_MAP

    def test_memory_enabled_kill_switch_in_env_map(self) -> None:
        assert ENV_MAP[("memory", "enabled")] == "MONKEYBOT_MEMORY_HOOK_ENABLED"
        assert ("memory", "engine") not in ENV_MAP


class TestMemoryEnabledConfig:
    def test_yaml_string_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from monkeybot.core.memory.config import memory_enabled_from_config

        monkeypatch.delenv("MONKEYBOT_MEMORY_HOOK_ENABLED", raising=False)
        cfg = tmp_path / "monkeybot.yaml"
        cfg.write_text('memory:\n  enabled: "false"\n', encoding="utf-8")
        assert memory_enabled_from_config(str(cfg)) is False

    def test_yaml_string_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from monkeybot.core.memory.config import memory_enabled_from_config

        monkeypatch.delenv("MONKEYBOT_MEMORY_HOOK_ENABLED", raising=False)
        cfg = tmp_path / "monkeybot.yaml"
        cfg.write_text('memory:\n  enabled: "true"\n', encoding="utf-8")
        assert memory_enabled_from_config(str(cfg)) is True


class TestVertexGoogleSearchConfig:
    def test_defaults_false_when_missing(self) -> None:
        assert vertex_google_search_enabled_from_config("/nonexistent/monkeybot.yaml") is False

    def test_reads_true_from_yaml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("web_search:\n  vertex_google_search: true\n", encoding="utf-8")
        assert vertex_google_search_enabled_from_config(str(config_path)) is True

    def test_reads_false_from_yaml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("web_search:\n  vertex_google_search: false\n", encoding="utf-8")
        assert vertex_google_search_enabled_from_config(str(config_path)) is False

    def test_rejects_non_boolean(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("web_search:\n  vertex_google_search: 0\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="must be true or false"):
            vertex_google_search_enabled_from_config(str(config_path))


class TestSubagentVertexGoogleSearchConfig:
    def test_defaults_false_when_missing(self) -> None:
        assert subagent_vertex_google_search_from_config("/nonexistent/monkeybot.yaml") is False

    def test_reads_true_from_yaml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("subagents:\n  vertex_google_search: true\n", encoding="utf-8")
        assert subagent_vertex_google_search_from_config(str(config_path)) is True

    def test_rejects_non_boolean(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("subagents:\n  vertex_google_search: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="must be true or false"):
            subagent_vertex_google_search_from_config(str(config_path))

    def test_subagent_vertex_google_search_not_in_env_map(self) -> None:
        assert ("subagents", "vertex_google_search") not in ENV_MAP
        assert ("subagent", "timeout_sec") not in ENV_MAP
        assert ("subagent", "max_turns") not in ENV_MAP
        assert ("subagent", "agent_md") not in ENV_MAP


class TestVertexAnthropicProvider:
    def test_normalize_model_provider_vertex_alias(self) -> None:
        assert normalize_model_provider("vertex") == "google_vertexai"
        assert normalize_model_provider("vertex-claude") == "vertex_anthropic"

    def test_get_provider_config_huggingface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_TOKEN", "hf_test")
        cfg = get_provider_config(provider="huggingface", model_name="meta-llama/Llama-3.1-8B-Instruct")
        assert cfg.provider.name == "huggingface"
        assert cfg.model == "meta-llama/Llama-3.1-8B-Instruct"

    def test_get_provider_config_ollama(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        cfg = get_provider_config(provider="ollama", model_name="llama3.1")
        assert cfg.provider.name == "ollama"
        assert cfg.model == "llama3.1"

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


class TestSubagentSettings:
    def _write_config(self, tmp_path: Path, yaml_text: str) -> Path:
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir(parents=True)
        path = cfg_dir / "monkeybot.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        return path

    def test_defaults_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(tmp_path, "model:\n  provider: gemini\n  name: test\n")
        settings = get_subagent_settings()
        assert settings.timeout_sec == 3600.0
        assert settings.max_turns == 1000
        assert settings.vertex_google_search is False

    def test_reads_defaults_and_personas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  timeout_sec: 120\n"
            "  max_turns: 10\n"
            "  personas:\n"
            "    - name: researcher\n"
            "      description: Research.\n"
            "      agent_md: ./agents/researcher.md\n",
        )
        settings = get_subagent_settings()
        assert settings.timeout_sec == 120.0
        assert settings.max_turns == 10
        assert get_subagent_settings().timeout_sec == 120.0
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "researcher"
        assert configs[0].agent_md == "./agents/researcher.md"

    def test_top_level_agent_md_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  agent_md: ./monkeybot_config/agents/default.md\n",
        )
        with pytest.raises(ConfigError, match="subagents.agent_md was removed"):
            get_subagent_settings()


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
            "  personas:\n"
            "    - name: content-intel\n"
            "      description: Research content.\n"
            "      skills:\n"
            "        - ./skills/content-intelligence/\n",
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
            "  personas:\n"
            "    - description: No name.\n"
            "      skills: []\n"
            "    - name: valid\n"
            "      description: Valid.\n"
            "      skills: []\n",
        )
        configs = get_subagent_configs()
        assert len(configs) == 1
        assert configs[0].name == "valid"

    def test_bare_list_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  - name: legacy\n"
            "    description: Old shape.\n",
        )
        with pytest.raises(ConfigError, match="bare list is no longer supported"):
            get_subagent_configs()


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
            "  personas:\n"
            "    - name: alpha\n"
            "      description: First.\n"
            "      agent_md: ./agents/alpha.md\n"
            "    - name: beta\n"
            "      description: Second.\n"
            "      agent_md: ./agents/beta.md\n",
        )
        reg = get_subagent_registry()
        assert set(reg) == {"alpha", "beta"}
        assert reg["alpha"].agent_md == "./agents/alpha.md"

    def test_duplicate_name_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  personas:\n"
            "    - name: dup\n"
            "      description: One.\n"
            "    - name: dup\n"
            "      description: Two.\n",
        )
        with pytest.raises(ConfigError, match="Duplicate subagent name"):
            get_subagent_registry()

    def test_prompt_file_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._write_config(
            tmp_path,
            "subagents:\n"
            "  personas:\n"
            "    - name: legacy\n"
            "      description: Legacy alias.\n"
            "      prompt_file: ./agents/legacy.md\n",
        )
        reg = get_subagent_registry()
        assert reg["legacy"].agent_md == "./agents/legacy.md"


class TestSandboxConfig:
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


class TestAutoSchemaConfig:
    def test_defaults_true_when_missing(self) -> None:
        assert auto_schema_enabled_from_config("/nonexistent/monkeybot.yaml") is True

    def test_reads_false_from_yaml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("paths:\n  auto_schema: false\n", encoding="utf-8")
        assert auto_schema_enabled_from_config(str(config_path)) is False

    def test_reads_true_from_yaml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("paths:\n  auto_schema: true\n", encoding="utf-8")
        assert auto_schema_enabled_from_config(str(config_path)) is True

    def test_rejects_non_boolean(self, tmp_path: Path) -> None:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text("paths:\n  auto_schema: 0\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="must be true or false"):
            auto_schema_enabled_from_config(str(config_path))

    def test_not_mapped_to_env(self) -> None:
        assert ("paths", "auto_schema") not in ENV_MAP


class TestReadDefaultLinesFixed:
    def test_agent_default_is_2000(self) -> None:
        assert AGENT_READ_DEFAULT_LINES == 2000

    def test_retired_from_yaml(self) -> None:
        assert "read_default_lines" in RETIRED_TOOLS_KEYS
        assert ("tools", "read_default_lines") not in ENV_MAP

    def test_yaml_key_warns_and_is_ignored(self) -> None:
        found = warn_retired_tools_keys({"tools": {"read_default_lines": 20000}})
        assert found == ["read_default_lines"]

    def test_read_file_tool_advertises_fixed_default(self) -> None:
        from monkeybot.core.context import _core_tool_defs

        tools = {t.name: t for t in _core_tool_defs()}
        read = tools["read_file"]
        assert str(AGENT_READ_DEFAULT_LINES) in read.description
        limit_desc = read.input_schema["properties"]["limit"]["description"]
        assert str(AGENT_READ_DEFAULT_LINES) in limit_desc
        assert "small" in limit_desc.lower() or "repeated" in limit_desc.lower()

    def test_default_limit_clamped_to_read_max(self, tmp_path: Path) -> None:
        from monkeybot.core.tools.workspace_service import WorkspaceFileService, WorkspaceSettings

        (tmp_path / "wide.txt").write_text(
            "\n".join(f"L{i}" for i in range(100)), encoding="utf-8"
        )
        svc = WorkspaceFileService(
            tmp_path,
            WorkspaceSettings(
                WORKSPACE_READ_MAX_LINES=40,
                WORKSPACE_READ_DEFAULT_LINES=AGENT_READ_DEFAULT_LINES,
            ),
        )
        result = svc.read_file("wide.txt")
        assert result["end_line"] - result["start_line"] + 1 == 40


class TestRealtimeConfig:
    def _write_config(self, tmp_path: Path, yaml_text: str) -> Path:
        config_path = tmp_path / "monkeybot.yaml"
        config_path.write_text(yaml_text, encoding="utf-8")
        return config_path

    def test_defaults_when_missing(self) -> None:
        cfg = get_realtime_config("/nonexistent/monkeybot.yaml")
        assert cfg.enabled is False
        assert cfg.websocket.enabled is True
        assert cfg.websocket.port is None
        assert cfg.audio.input_format == "pcm_s16le_24khz_mono"
        assert cfg.session.max_duration_sec == 1800

    def test_parses_realtime_mode(self, tmp_path: Path) -> None:
        path = self._write_config(
            tmp_path,
            "harness:\n  mode: realtime\n"
            "realtime:\n  session:\n    max_duration_sec: 900\n",
        )
        cfg = get_realtime_config(str(path))
        assert cfg.enabled is True
        assert cfg.session.max_duration_sec == 900

    def test_parses_nested_values(self, tmp_path: Path) -> None:
        path = self._write_config(
            tmp_path,
            "harness:\n  mode: realtime\n"
            "realtime:\n"
            "  websocket:\n    enabled: false\n    port: 9090\n"
            "  audio:\n    input_format: pcm_s16le_16khz_mono\n"
            "    chunk_ms: 100\n"
            "  session:\n    max_concurrent_sessions: 50\n",
        )
        cfg = get_realtime_config(str(path))
        assert cfg.enabled is True
        assert cfg.websocket.enabled is False
        assert cfg.websocket.port == 9090
        assert cfg.audio.input_format == "pcm_s16le_16khz_mono"
        assert cfg.audio.chunk_ms == 100
        assert cfg.session.max_concurrent_sessions == 50

    def test_parses_realtime_model_override(self, tmp_path: Path) -> None:
        path = self._write_config(
            tmp_path,
            "harness:\n  mode: realtime\n"
            "model:\n  provider: google_genai\n  name: gemini-2.5-flash\n"
            "realtime:\n"
            "  model:\n    name: gemini-3.1-flash-live-preview\n"
            "    provider: google_genai\n",
        )
        cfg = get_realtime_config(str(path))
        assert cfg.model.name == "gemini-3.1-flash-live-preview"
        assert cfg.model.provider == "google_genai"


class TestHarnessModeValidation:
    def test_default_mode_is_turn_based(self) -> None:
        validate_monkeybot_yaml_doc({"model": {"provider": "gemini", "name": "x"}})

    def test_accepts_realtime_with_gemini(self) -> None:
        validate_monkeybot_yaml_doc(
            {"harness": {"mode": "realtime"}, "model": {"provider": "gemini", "name": "x"}}
        )

    def test_rejects_realtime_with_non_gemini(self) -> None:
        with pytest.raises(ConfigError, match="not a Gemini family provider"):
            validate_monkeybot_yaml_doc(
                {
                    "harness": {"mode": "realtime"},
                    "model": {"provider": "openai", "name": "x"},
                }
            )

    def test_rejects_invalid_mode(self) -> None:
        with pytest.raises(ConfigError, match="not supported"):
            validate_monkeybot_yaml_doc({"harness": {"mode": "both"}})

    def test_rejects_invalid_audio_format(self) -> None:
        with pytest.raises(ConfigError, match="not supported"):
            validate_monkeybot_yaml_doc(
                {
                    "harness": {"mode": "turn_based"},
                    "realtime": {"audio": {"input_format": "mp3"}},
                }
            )

    def test_rejects_non_positive_session_value(self) -> None:
        with pytest.raises(ConfigError, match="positive number"):
            validate_monkeybot_yaml_doc(
                {"realtime": {"session": {"max_duration_sec": -1}}}
            )
