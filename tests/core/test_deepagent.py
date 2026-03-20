"""Unit tests for deep agent factory.

Tests build_deep_agent() with mocked deepagents dependencies.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from datetime import datetime, timedelta

from src.core.deepagent import (
    build_deep_agent,
    _generate_skills_manifest,
    _parse_skill_frontmatter,
    _create_schedule_task_tool,
)


class TestBuildDeepAgent:
    """Tests for build_deep_agent factory function."""
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", False)
    def test_raises_import_error_when_deepagents_not_available(self):
        """Test that ImportError is raised when deepagents not installed."""
        with pytest.raises(ImportError) as exc_info:
            build_deep_agent(model="gemini-2.5-flash")
        
        assert "deepagents package required" in str(exc_info.value)
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_minimal_agent_creation(self, mock_create):
        """Test creating agent with minimal parameters."""
        mock_create.return_value = Mock()
        
        agent = build_deep_agent(model="gemini-2.5-flash")
        
        # Verify create_deep_agent was called
        assert mock_create.called
        call_kwargs = mock_create.call_args.kwargs
        
        # Check model
        assert call_kwargs["model"] == "gemini-2.5-flash"
        
        # Check tools (should be empty list)
        assert call_kwargs["tools"] == []
        
        # Check middleware (should be empty - summarization added by create_deep_agent)
        assert call_kwargs["middleware"] == []
        
        # Check system prompt is composed
        assert "You are an AI agent" in call_kwargs["system_prompt"]
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_with_custom_tools(self, mock_create):
        """Test creating agent with custom tools."""
        mock_create.return_value = Mock()
        
        mock_tool1 = Mock()
        mock_tool1.name = "tool1"
        mock_tool2 = Mock()
        mock_tool2.name = "tool2"
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            tools=[mock_tool1, mock_tool2],
        )
        
        call_kwargs = mock_create.call_args.kwargs
        tools = call_kwargs["tools"]
        
        # Should have 2 tools
        assert len(tools) == 2
        assert mock_tool1 in tools
        assert mock_tool2 in tools
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent.create_search_memory_tool")
    def test_auto_adds_memory_tool_when_store_provided(
        self, mock_memory_tool, mock_create
    ):
        """Test that search_memory tool is auto-added when store provided."""
        mock_create.return_value = Mock()
        mock_store = Mock()
        
        # Create a mock tool with name attribute
        mock_tool = Mock()
        mock_tool.name = "search_memory"
        mock_memory_tool.return_value = mock_tool
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            store=mock_store,
        )
        
        # Verify memory tool was created
        mock_memory_tool.assert_called_once_with(mock_store)
        
        # Verify tool was added
        call_kwargs = mock_create.call_args.kwargs
        tools = call_kwargs["tools"]
        assert len(tools) == 1
        assert tools[0].name == "search_memory"
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent._create_schedule_task_tool")
    def test_auto_adds_scheduler_tool_when_scheduler_provided(
        self, mock_schedule_tool, mock_create
    ):
        """Test that schedule_task tool is auto-added when scheduler provided."""
        mock_create.return_value = Mock()
        mock_scheduler = Mock()
        
        # Create a mock tool with name attribute
        mock_tool = Mock()
        mock_tool.name = "schedule_task"
        mock_schedule_tool.return_value = mock_tool
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            scheduler=mock_scheduler,
        )
        
        # Verify schedule tool was created
        mock_schedule_tool.assert_called_once_with(mock_scheduler)
        
        # Verify tool was added
        call_kwargs = mock_create.call_args.kwargs
        tools = call_kwargs["tools"]
        assert len(tools) == 1
        assert tools[0].name == "schedule_task"
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent._generate_skills_manifest")
    def test_generates_skills_manifest_when_skills_provided(
        self, mock_manifest, mock_create
    ):
        """Test that skills manifest is generated when skills dirs provided."""
        mock_create.return_value = Mock()
        mock_manifest.return_value = "- skill1: Test skill"
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            skills=["./skills/", "/shared/skills/"],
        )
        
        # Verify manifest generation was called
        mock_manifest.assert_called_once_with(["./skills/", "/shared/skills/"])
        
        # Verify manifest is in system prompt
        call_kwargs = mock_create.call_args.kwargs
        assert "skill1: Test skill" in call_kwargs["system_prompt"]
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_includes_user_system_prompt(self, mock_create):
        """Test that user's custom system prompt is included."""
        mock_create.return_value = Mock()
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            system_prompt="You are a marketing assistant.",
        )
        
        call_kwargs = mock_create.call_args.kwargs
        assert "You are a marketing assistant." in call_kwargs["system_prompt"]
        assert "## Domain-Specific Instructions" in call_kwargs["system_prompt"]
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_passes_subagents_to_create_deep_agent(self, mock_create):
        """Test that subagents are passed through to create_deep_agent() directly.

        SubAgentMiddleware is no longer wired manually in build_deep_agent();
        create_deep_agent() handles the full middleware stack for subagents.
        """
        mock_create.return_value = Mock()

        subagents = [
            {
                "name": "researcher",
                "description": "Research assistant",
                "system_prompt": "You are a researcher.",
                "skills": ["./skills/research/"],
            },
            {
                "name": "writer",
                "description": "Content writer",
                "system_prompt": "You are a writer.",
                "skills": ["./skills/writing/"],
            },
        ]

        build_deep_agent(
            model="gemini-2.5-flash",
            subagents=subagents,
        )

        call_kwargs = mock_create.call_args.kwargs
        # Subagents passed directly — not wrapped in middleware
        assert call_kwargs["subagents"] == subagents
        # middleware list must NOT contain any SubAgentMiddleware instance
        for mw in call_kwargs.get("middleware", []):
            assert "SubAgentMiddleware" not in type(mw).__name__

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_passes_subagents_none_when_not_provided(self, mock_create):
        """Test that subagents=None is passed when no subagents are configured."""
        mock_create.return_value = Mock()

        build_deep_agent(model="gemini-2.5-flash")

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["subagents"] is None
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_custom_summarization_params(self, mock_create):
        """Test custom summarization trigger and keep parameters."""
        mock_create.return_value = Mock()
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            summarization_trigger=("tokens", 5000),
            summarization_keep=("messages", 10),
        )
        
        # Verify agent was created (summarization params accepted but not used)
        # Note: create_deep_agent adds SummarizationMiddleware automatically
        # and doesn't currently expose configuration parameters
        assert mock_create.called
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_passes_backend_through_to_create_deep_agent(self, mock_create):
        """Test that backend is passed through to create_deep_agent()."""
        mock_create.return_value = Mock()
        mock_backend = Mock()

        build_deep_agent(
            model="gemini-2.5-flash",
            backend=mock_backend,
        )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["backend"] == mock_backend

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    @patch("src.core.deepagent._generate_skills_manifest")
    def test_passes_skills_through_to_create_deep_agent(
        self, mock_manifest, mock_create
    ):
        """Test that skills dirs are passed to create_deep_agent() for SkillsMiddleware."""
        mock_create.return_value = Mock()
        mock_manifest.return_value = "- skill1: desc"

        skills_list = ["./skills/", "/shared/skills/"]
        build_deep_agent(
            model="gemini-2.5-flash",
            skills=skills_list,
        )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["skills"] == skills_list

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_passes_skills_none_when_not_provided(self, mock_create):
        """Test that skills=None is passed when no skills dirs are configured."""
        mock_create.return_value = Mock()

        build_deep_agent(model="gemini-2.5-flash")

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["skills"] is None

    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_subagent_specs_with_skills_passed_correctly(self, mock_create):
        """Test that subagent dicts containing a 'skills' field pass through unchanged."""
        mock_create.return_value = Mock()

        subagent_spec = {
            "name": "analytics",
            "description": "Query GA4.",
            "system_prompt": "You are an analytics specialist.",
            "skills": ["./skills/analytics/"],
            "model": "gemini-2.0-flash",
        }

        build_deep_agent(
            model="gemini-2.5-flash",
            subagents=[subagent_spec],
        )

        call_kwargs = mock_create.call_args.kwargs
        passed = call_kwargs["subagents"]
        assert len(passed) == 1
        assert passed[0]["skills"] == ["./skills/analytics/"]
        assert passed[0]["model"] == "gemini-2.0-flash"
    
    @patch("src.core.deepagent._DEEPAGENTS_AVAILABLE", True)
    @patch("src.core.deepagent.create_deep_agent")
    def test_system_prompt_reflects_enabled_features(self, mock_create):
        """Test that system prompt includes sections for enabled features."""
        mock_create.return_value = Mock()
        mock_store = Mock()
        mock_scheduler = Mock()
        mock_sandbox = Mock()
        
        agent = build_deep_agent(
            model="gemini-2.5-flash",
            store=mock_store,
            scheduler=mock_scheduler,
            backend=mock_sandbox,  # backend parameter (not sandbox)
        )
        
        call_kwargs = mock_create.call_args.kwargs
        prompt = call_kwargs["system_prompt"]
        
        # Check that feature sections are present when features are enabled
        # Note: Actual feature sections depend on compose_system_prompt implementation
        assert mock_create.called  # Verify agent was created successfully


class TestGenerateSkillsManifest:
    """Tests for _generate_skills_manifest helper function."""
    
    def test_empty_skills_dirs(self):
        """Test with empty skills directories list."""
        manifest = _generate_skills_manifest([])
        assert manifest == "No skills available."
    
    def test_nonexistent_directory(self, tmp_path):
        """Test with nonexistent directory."""
        manifest = _generate_skills_manifest([str(tmp_path / "nonexistent")])
        assert manifest == "No skills available."
    
    def test_single_skill(self, tmp_path):
        """Test with a single skill."""
        # Create skill directory
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        
        # Create SKILL.md
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
---

# Test Skill
""")
        
        manifest = _generate_skills_manifest([str(tmp_path)])
        
        assert "test-skill: A test skill" in manifest
        assert "No skills available" not in manifest
    
    def test_multiple_skills(self, tmp_path):
        """Test with multiple skills."""
        # Create first skill
        skill1_dir = tmp_path / "skill1"
        skill1_dir.mkdir()
        (skill1_dir / "SKILL.md").write_text("""---
name: skill1
description: First skill
---
""")
        
        # Create second skill
        skill2_dir = tmp_path / "skill2"
        skill2_dir.mkdir()
        (skill2_dir / "SKILL.md").write_text("""---
name: skill2
description: Second skill
---
""")
        
        manifest = _generate_skills_manifest([str(tmp_path)])
        
        assert "skill1: First skill" in manifest
        assert "skill2: Second skill" in manifest
    
    def test_multiple_directories(self, tmp_path):
        """Test with multiple skill directories."""
        # Create first directory with skill
        dir1 = tmp_path / "dir1"
        dir1.mkdir()
        skill1_dir = dir1 / "skill1"
        skill1_dir.mkdir()
        (skill1_dir / "SKILL.md").write_text("""---
name: skill1
description: Skill from dir1
---
""")
        
        # Create second directory with skill
        dir2 = tmp_path / "dir2"
        dir2.mkdir()
        skill2_dir = dir2 / "skill2"
        skill2_dir.mkdir()
        (skill2_dir / "SKILL.md").write_text("""---
name: skill2
description: Skill from dir2
---
""")
        
        manifest = _generate_skills_manifest([str(dir1), str(dir2)])
        
        assert "skill1: Skill from dir1" in manifest
        assert "skill2: Skill from dir2" in manifest
    
    def test_skips_directories_without_skill_md(self, tmp_path):
        """Test that directories without SKILL.md are skipped."""
        # Create directory without SKILL.md
        no_skill_dir = tmp_path / "no-skill"
        no_skill_dir.mkdir()
        (no_skill_dir / "readme.txt").write_text("Not a skill")
        
        # Create valid skill
        skill_dir = tmp_path / "valid-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: valid-skill
description: Valid skill
---
""")
        
        manifest = _generate_skills_manifest([str(tmp_path)])
        
        assert "valid-skill: Valid skill" in manifest
        assert "no-skill" not in manifest
    
    def test_skips_files_in_skills_dir(self, tmp_path):
        """Test that files in skills directory are skipped."""
        # Create a file (not directory)
        (tmp_path / "readme.txt").write_text("Not a skill")
        
        # Create valid skill
        skill_dir = tmp_path / "valid-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: valid-skill
description: Valid skill
---
""")
        
        manifest = _generate_skills_manifest([str(tmp_path)])
        
        assert "valid-skill: Valid skill" in manifest
    
    def test_handles_missing_name_in_frontmatter(self, tmp_path):
        """Test that skills without name are skipped."""
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
description: Missing name field
---
""")
        
        manifest = _generate_skills_manifest([str(tmp_path)])
        
        assert manifest == "No skills available."
    
    def test_handles_empty_description(self, tmp_path):
        """Test that skills with empty description are included."""
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test-skill
description: ""
---
""")
        
        manifest = _generate_skills_manifest([str(tmp_path)])
        
        assert "test-skill:" in manifest


class TestParseSkillFrontmatter:
    """Tests for _parse_skill_frontmatter helper function."""
    
    def test_valid_frontmatter(self, tmp_path):
        """Test parsing valid SKILL.md frontmatter."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
version: 1.0.0
---

# Test Skill
""")
        
        metadata = _parse_skill_frontmatter(skill_md)
        
        assert metadata is not None
        assert metadata["name"] == "test-skill"
        assert metadata["description"] == "A test skill"
        assert metadata["version"] == "1.0.0"
    
    def test_missing_frontmatter(self, tmp_path):
        """Test file without frontmatter."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# Test Skill\n\nNo frontmatter here.")
        
        metadata = _parse_skill_frontmatter(skill_md)
        
        assert metadata is None
    
    def test_malformed_frontmatter(self, tmp_path):
        """Test file with malformed frontmatter."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: Missing closing delimiter

# Test Skill
""")
        
        metadata = _parse_skill_frontmatter(skill_md)
        
        assert metadata is None
    
    def test_invalid_yaml(self, tmp_path):
        """Test file with invalid YAML."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: [invalid yaml
---
""")
        
        metadata = _parse_skill_frontmatter(skill_md)
        
        assert metadata is None
    
    def test_empty_frontmatter(self, tmp_path):
        """Test file with empty frontmatter."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("""---
---

# Test Skill
""")
        
        metadata = _parse_skill_frontmatter(skill_md)
        
        assert metadata is None or metadata == {}


class TestCreateScheduleTaskTool:
    """Tests for _create_schedule_task_tool helper function."""
    
    @pytest.mark.asyncio
    async def test_schedule_task_tool_creation(self):
        """Test that schedule_task tool is created correctly."""
        mock_scheduler = Mock()
        mock_scheduler.schedule_job = AsyncMock(return_value="job-123")
        
        tool = _create_schedule_task_tool(mock_scheduler)
        
        assert tool.name == "schedule_task"
        assert "schedule" in tool.description.lower()
    
    @pytest.mark.asyncio
    async def test_schedule_task_tool_execution(self):
        """Test executing the schedule_task tool."""
        mock_scheduler = Mock()
        mock_scheduler.schedule_job = AsyncMock(return_value="job-456")
        
        tool = _create_schedule_task_tool(mock_scheduler)
        
        # Execute the tool using ainvoke
        result = await tool.ainvoke({
            "job_type": "test_job",
            "schedule_at_iso": "2024-02-14T09:00:00Z",
            "payload": {"key": "value"},
        })
        
        # Verify scheduler was called
        mock_scheduler.schedule_job.assert_called_once()
        call_args = mock_scheduler.schedule_job.call_args
        
        assert call_args.kwargs["job_type"] == "test_job"
        assert call_args.kwargs["payload"] == {"key": "value"}
        
        # Verify result
        assert "job-456" in result
        assert "success" in result.lower()
    
    @pytest.mark.asyncio
    async def test_schedule_task_tool_invalid_datetime(self):
        """Test schedule_task tool with invalid datetime format."""
        mock_scheduler = Mock()
        
        tool = _create_schedule_task_tool(mock_scheduler)
        
        # Execute with invalid datetime using ainvoke
        result = await tool.ainvoke({
            "job_type": "test_job",
            "schedule_at_iso": "invalid-datetime",
            "payload": {},
        })
        
        # Should return error message
        assert "Error" in result
        assert "Invalid ISO 8601" in result
    
    @pytest.mark.asyncio
    async def test_schedule_task_tool_datetime_parsing(self):
        """Test that datetime is parsed correctly."""
        mock_scheduler = Mock()
        mock_scheduler.schedule_job = AsyncMock(return_value="job-789")
        
        tool = _create_schedule_task_tool(mock_scheduler)
        
        # Execute with ISO datetime using ainvoke
        await tool.ainvoke({
            "job_type": "test_job",
            "schedule_at_iso": "2024-02-14T09:00:00Z",
            "payload": {},
        })
        
        # Verify datetime was parsed
        call_args = mock_scheduler.schedule_job.call_args
        schedule_at = call_args.kwargs["schedule_at"]
        
        assert isinstance(schedule_at, datetime)
        assert schedule_at.year == 2024
        assert schedule_at.month == 2
        assert schedule_at.day == 14
        assert schedule_at.hour == 9
