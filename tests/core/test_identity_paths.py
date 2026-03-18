"""Tests for IDENTITY.md + absolute path injection (story-2-identity-path-injection)."""
from unittest.mock import MagicMock, patch

import pytest

from src.core.prompt import compose_system_prompt
from src.core.deepagent import build_deep_agent


class TestIdentityInPrompt:
    """Tests for IDENTITY section rendering in compose_system_prompt."""

    def test_identity_content_renders_header(self):
        """Non-empty identity_content → header present in prompt."""
        prompt = compose_system_prompt(identity_content="You are the ops lead.")
        assert "[IDENTITY — ROLE & OPERATIONAL CONTEXT]" in prompt

    def test_identity_content_renders_body(self):
        """Non-empty identity_content → content text present in prompt."""
        prompt = compose_system_prompt(identity_content="You are the ops lead.")
        assert "You are the ops lead." in prompt

    def test_empty_identity_omits_section(self):
        """Empty identity_content → section absent from prompt."""
        prompt = compose_system_prompt(identity_content="")
        assert "[IDENTITY — ROLE & OPERATIONAL CONTEXT]" not in prompt

    def test_identity_before_soul_in_output(self):
        """identity_content appears before soul_content in final output."""
        prompt = compose_system_prompt(
            identity_content="Ops lead identity.",
            soul_content="Core values here.",
        )
        identity_pos = prompt.find("[IDENTITY — ROLE & OPERATIONAL CONTEXT]")
        soul_pos = prompt.find("[IDENTITY — HIGHEST PRIORITY]")
        assert identity_pos != -1
        assert soul_pos != -1
        assert identity_pos < soul_pos

    def test_identity_present_without_soul(self):
        """Identity section renders even when soul_content is empty."""
        prompt = compose_system_prompt(identity_content="Role: automation lead.")
        assert "[IDENTITY — ROLE & OPERATIONAL CONTEXT]" in prompt
        assert "[IDENTITY — HIGHEST PRIORITY]" not in prompt

    def test_identity_does_not_conflict_with_soul(self):
        """Both identity and soul can coexist independently."""
        prompt = compose_system_prompt(
            identity_content="My role.",
            soul_content="My values.",
        )
        assert "[IDENTITY — ROLE & OPERATIONAL CONTEXT]" in prompt
        assert "[IDENTITY — HIGHEST PRIORITY]" in prompt


class TestIndexInPrompt:
    """Tests for INDEX.md section rendering in compose_system_prompt."""

    def test_index_content_renders_header(self):
        """Non-empty index_content → '## Memory Index' in prompt."""
        prompt = compose_system_prompt(index_content="- notes/ideas.md\n- logs/run.md")
        assert "## Memory Index" in prompt

    def test_index_content_renders_body(self):
        """Index content text appears in prompt."""
        prompt = compose_system_prompt(index_content="- notes/ideas.md")
        assert "notes/ideas.md" in prompt

    def test_empty_index_omits_section(self):
        """Empty index_content → section absent."""
        prompt = compose_system_prompt(index_content="")
        assert "## Memory Index" not in prompt

    def test_memory_dir_appears_in_index_section(self):
        """memory_dir value appears in the index section instruction."""
        prompt = compose_system_prompt(
            index_content="- file.md",
            memory_dir="/app/data/memory",
        )
        assert "/app/data/memory" in prompt

    def test_memory_dir_appears_in_filesystem_section(self):
        """memory_dir value appears in the filesystem memory section."""
        prompt = compose_system_prompt(
            has_filesystem_memory=True,
            memory_dir="/custom/memory",
        )
        assert "/custom/memory" in prompt

    def test_default_memory_dir_used_when_not_specified(self):
        """Default memory_dir is used when param is omitted."""
        prompt = compose_system_prompt(
            index_content="- entry.md",
        )
        assert "./data/memory" in prompt


class TestResolvedPaths:
    """Tests for resolved paths section rendering in compose_system_prompt."""

    def test_resolved_paths_renders_key_value(self):
        """resolved_paths dict → 'KEY=value' lines in prompt."""
        prompt = compose_system_prompt(
            resolved_paths={"MEMORY_DIR": "/app/data/memory"}
        )
        assert "MEMORY_DIR=/app/data/memory" in prompt

    def test_resolved_paths_renders_header(self):
        """resolved_paths present → '## Resolved Filesystem Paths' in prompt."""
        prompt = compose_system_prompt(
            resolved_paths={"MEMORY_DIR": "/app/data/memory"}
        )
        assert "## Resolved Filesystem Paths" in prompt

    def test_resolved_paths_none_omits_section(self):
        """resolved_paths=None → section absent from prompt."""
        prompt = compose_system_prompt(resolved_paths=None)
        assert "## Resolved Filesystem Paths" not in prompt

    def test_resolved_paths_multiple_keys(self):
        """Multiple entries all appear in prompt."""
        paths = {
            "MEMORY_DIR": "/app/data/memory",
            "SKILLS_DIR": "/app/skills",
            "INDEX_MD": "/app/data/memory/INDEX.md",
        }
        prompt = compose_system_prompt(resolved_paths=paths)
        assert "MEMORY_DIR=/app/data/memory" in prompt
        assert "SKILLS_DIR=/app/skills" in prompt
        assert "INDEX_MD=/app/data/memory/INDEX.md" in prompt

    def test_resolved_paths_empty_dict_omits_section(self):
        """Empty resolved_paths dict → section absent."""
        prompt = compose_system_prompt(resolved_paths={})
        assert "## Resolved Filesystem Paths" not in prompt


class TestBuildDeepAgentIdentity:
    """Tests for identity_file loading and INDEX.md auto-loading in build_deep_agent."""

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_identity_file_content_passed_to_compose(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """Explicit identity_file with content → identity_content passed to compose."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)

        identity_path = tmp_path / "IDENTITY.md"
        identity_path.write_text("You are the ops automation lead.")

        build_deep_agent(model="gemini-2.5-flash", identity_file=str(identity_path))

        _, kwargs = mock_compose.call_args
        assert kwargs.get("identity_content") == "You are the ops automation lead."

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_missing_identity_file_passes_empty_string(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """Missing identity_file → identity_content='' (no crash)."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)

        build_deep_agent(
            model="gemini-2.5-flash",
            identity_file=str(tmp_path / "NONEXISTENT_IDENTITY.md"),
        )

        _, kwargs = mock_compose.call_args
        assert kwargs.get("identity_content") == ""

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_identity_md_auto_loaded_from_cwd(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """No identity_file param → IDENTITY.md auto-loaded from cwd."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)
        (tmp_path / "IDENTITY.md").write_text("Auto-loaded identity.")

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        assert kwargs.get("identity_content") == "Auto-loaded identity."

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_index_md_auto_loaded_when_exists(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """INDEX.md in memory dir → index_content loaded and passed to compose."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)

        memory_dir = tmp_path / "data" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "INDEX.md").write_text("- notes/ideas.md\n- logs/run.md")
        monkeypatch.setenv("MEMORY_DIR", str(memory_dir))

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        assert kwargs.get("index_content") == "- notes/ideas.md\n- logs/run.md"

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_missing_index_md_no_error(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """Missing INDEX.md → no error, index_content=''."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        assert kwargs.get("index_content") == ""

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_resolved_paths_passed_to_compose(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """resolved_paths dict is passed to compose_system_prompt."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        resolved = kwargs.get("resolved_paths")
        assert isinstance(resolved, dict)
        assert "MEMORY_DIR" in resolved
        assert "SKILLS_DIR" in resolved
        assert "INDEX_MD" in resolved
        assert "USER_MD" in resolved

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_resolved_paths_include_soul_when_present(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """SOUL.md present → SOUL_FILE key in resolved_paths."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)
        (tmp_path / "SOUL.md").write_text("Values.")

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        resolved = kwargs.get("resolved_paths", {})
        assert "SOUL_FILE" in resolved

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_resolved_paths_include_identity_when_present(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """IDENTITY.md present → IDENTITY_FILE key in resolved_paths."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)
        (tmp_path / "IDENTITY.md").write_text("Role.")

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        resolved = kwargs.get("resolved_paths", {})
        assert "IDENTITY_FILE" in resolved

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.compose_system_prompt")
    def test_identity_file_env_var_used_when_set(
        self, mock_compose, mock_create, tmp_path, monkeypatch
    ):
        """IDENTITY_FILE env var used when no explicit identity_file param."""
        mock_create.return_value = MagicMock()
        mock_compose.return_value = "composed"
        monkeypatch.chdir(tmp_path)

        env_identity = tmp_path / "env_identity.md"
        env_identity.write_text("From env var.")
        monkeypatch.setenv("IDENTITY_FILE", str(env_identity))

        build_deep_agent(model="gemini-2.5-flash")

        _, kwargs = mock_compose.call_args
        assert kwargs.get("identity_content") == "From env var."
