"""
Tests for Skills Loader.

Verifies skill discovery, SKILL.md parsing, and error handling.
"""

import pytest
from pathlib import Path

from monkeybot.skills.loader import SkillLoader


@pytest.fixture
def skills_dir(tmp_path):
    """Create temporary skills directory with test skills."""
    skills = tmp_path / "skills"
    skills.mkdir()
    return skills


@pytest.fixture
def valid_skill(skills_dir):
    """Create a valid skill with SKILL.md and entry point."""
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir()

    # Create SKILL.md
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("""---
name: test-skill
description: "Test skill for unit tests"
metadata:
  monkeybot:
    requires:
      bins: ["cat"]
---

# Test Skill
This is a test skill.
""")

    # Create entry point
    entry_point = skill_dir / "test_skill.py"
    entry_point.write_text("# Test skill implementation")

    return skill_dir


class TestSkillLoaderBasics:
    """Basic functionality tests for SkillLoader."""

    def test_loader_initialization(self, skills_dir):
        """Test that loader initializes correctly."""
        loader = SkillLoader(str(skills_dir))

        assert loader.skills_dir == Path(skills_dir)
        assert loader.skills == {}

    def test_load_valid_skill(self, skills_dir, valid_skill):
        """Test loading a valid skill."""
        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert "test-skill" in skills
        assert skills["test-skill"]["description"] == "Test skill for unit tests"
        assert "test_skill.py" in skills["test-skill"]["entry_point"]

    def test_load_multiple_skills(self, skills_dir):
        """Test loading multiple skills."""
        # Create two skills
        for skill_name in ["skill-one", "skill-two"]:
            skill_dir = skills_dir / skill_name
            skill_dir.mkdir()

            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(f"""---
name: {skill_name}
description: "Test {skill_name}"
---

# {skill_name}
""")

            entry_point = skill_dir / f"{skill_name.replace('-', '_')}.py"
            entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert len(skills) == 2
        assert "skill-one" in skills
        assert "skill-two" in skills


class TestSkillLoaderErrorHandling:
    """Error handling tests for SkillLoader."""

    def test_missing_skills_directory(self, tmp_path):
        """Test handling of missing skills directory."""
        missing_dir = tmp_path / "nonexistent"
        loader = SkillLoader(str(missing_dir))
        skills = loader.load_skills()

        assert skills == {}

    def test_skill_without_skill_md(self, skills_dir):
        """Test that skills without SKILL.md are skipped."""
        skill_dir = skills_dir / "invalid-skill"
        skill_dir.mkdir()

        # No SKILL.md created

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert "invalid-skill" not in skills

    def test_skill_with_invalid_yaml(self, skills_dir):
        """Test that skills with invalid YAML are skipped."""
        skill_dir = skills_dir / "bad-yaml-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: bad-yaml-skill
description: "Missing closing quote
invalid: yaml: syntax:
---

# Bad YAML
""")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert "bad-yaml-skill" not in skills

    def test_skill_missing_name_field(self, skills_dir):
        """Test that skills without 'name' field are skipped."""
        skill_dir = skills_dir / "no-name-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
description: "Skill without name"
---

# No Name Skill
""")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert len(skills) == 0

    def test_skill_without_frontmatter(self, skills_dir):
        """Test that skills without YAML frontmatter are skipped."""
        skill_dir = skills_dir / "no-frontmatter-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""# No Frontmatter Skill

This skill has no YAML frontmatter.
""")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert "no-frontmatter-skill" not in skills

    def test_duplicate_skill_names(self, skills_dir):
        """Test that duplicate skill names use first occurrence."""
        # Create two skills with same name
        for i in range(2):
            skill_dir = skills_dir / f"dup-skill-{i}"
            skill_dir.mkdir()

            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(f"""---
name: duplicate-name
description: "Duplicate {i}"
---

# Duplicate
""")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        # Only one should be loaded
        assert len(skills) == 1
        assert skills["duplicate-name"]["description"] == "Duplicate 0"

    def test_skill_missing_entry_point(self, skills_dir):
        """Test that skills without entry point are loaded with warning."""
        skill_dir = skills_dir / "no-entry-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: no-entry-skill
description: "No entry point"
---

# No Entry
""")

        # No entry point file created

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        # Should still load metadata
        assert "no-entry-skill" in skills
        assert skills["no-entry-skill"]["description"] == "No entry point"


class TestSkillLoaderMetadata:
    """Tests for metadata extraction from SKILL.md."""

    def test_metadata_extraction(self, skills_dir):
        """Test that all metadata fields are extracted correctly."""
        skill_dir = skills_dir / "full-metadata-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: full-metadata-skill
description: "Skill with full metadata"
metadata:
  monkeybot:
    requires:
      bins: ["cat", "ls"]
      files: ["./data/"]
    install:
      - id: test
        kind: manual
---

# Full Metadata Skill
""")

        entry_point = skill_dir / "full_metadata_skill.py"
        entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        skill = skills["full-metadata-skill"]
        assert skill["description"] == "Skill with full metadata"
        assert "metadata" in skill["metadata"]
        assert "monkeybot" in skill["metadata"]["metadata"]

    def test_minimal_metadata(self, skills_dir):
        """Test skill with minimal required metadata."""
        skill_dir = skills_dir / "minimal-skill"
        skill_dir.mkdir()

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: minimal-skill
description: "Minimal metadata"
---

# Minimal
""")

        entry_point = skill_dir / "minimal_skill.py"
        entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert "minimal-skill" in skills
        assert skills["minimal-skill"]["description"] == "Minimal metadata"


class TestSkillLoaderFilesystemHandling:
    """Tests for filesystem edge cases."""

    def test_ignore_non_directory_items(self, skills_dir):
        """Test that non-directory items in skills dir are ignored."""
        # Create a file in skills directory (not a directory)
        (skills_dir / "readme.txt").write_text("Not a skill")

        # Create a valid skill
        skill_dir = skills_dir / "valid-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: valid-skill
description: "Valid"
---
""")

        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert "valid-skill" in skills
        assert len(skills) == 1

    def test_empty_skills_directory(self, skills_dir):
        """Test handling of empty skills directory."""
        loader = SkillLoader(str(skills_dir))
        skills = loader.load_skills()

        assert skills == {}


class TestSkillLoaderToolSchemas:
    """Tests for tool schema generation."""

    def test_get_tool_schemas_with_valid_skills(self, skills_dir):
        """Test: Valid skills generate correct tool schemas."""
        # Create skill with good description
        skill_dir = skills_dir / "test-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: "This is a test skill with a good description that is long enough for routing."
---
# Test Skill
""")

        entry_point = skill_dir / "test_skill.py"
        entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        loader.load_skills()
        schemas = loader.get_tool_schemas()

        assert len(schemas) == 1
        assert schemas[0]["name"] == "test-skill"
        assert "test skill" in schemas[0]["description"].lower()
        assert schemas[0]["parameters"]["type"] == "object"
        assert schemas[0]["parameters"]["properties"] == {}
        assert schemas[0]["parameters"]["required"] == []

    def test_get_tool_schemas_with_short_description(self, skills_dir, caplog):
        """Test: Short descriptions trigger warning."""
        # Create skill with short description
        skill_dir = skills_dir / "short-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: short-skill
description: "Short"
---
# Short
""")

        entry_point = skill_dir / "short_skill.py"
        entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        loader.load_skills()
        schemas = loader.get_tool_schemas()

        # Should still generate schema
        assert len(schemas) == 1

        # But should log warning
        assert "short description" in caplog.text.lower()

    def test_get_tool_schemas_with_no_skills(self, skills_dir):
        """Test: Empty skills directory returns empty list."""
        loader = SkillLoader(str(skills_dir))
        loader.load_skills()
        schemas = loader.get_tool_schemas()

        assert schemas == []

    def test_get_tool_schemas_with_multiple_skills(self, skills_dir):
        """Test: Multiple skills generate multiple schemas."""
        # Create 3 skills
        for i in range(3):
            skill_dir = skills_dir / f"skill-{i}"
            skill_dir.mkdir()
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(f"""---
name: skill-{i}
description: "This is skill {i} with a description for routing."
---
# Skill {i}
""")
            entry_point = skill_dir / f"skill_{i}.py"
            entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        loader.load_skills()
        schemas = loader.get_tool_schemas()

        assert len(schemas) == 3
        assert set([s["name"] for s in schemas]) == {"skill-0", "skill-1", "skill-2"}

    def test_get_tool_schemas_before_loading_skills(self, skills_dir):
        """Test: Calling get_tool_schemas before load_skills returns empty list."""
        loader = SkillLoader(str(skills_dir))
        # Don't call load_skills()
        schemas = loader.get_tool_schemas()

        assert schemas == []

    def test_tool_schema_structure(self, skills_dir):
        """Test: Tool schemas have correct structure for LLM tool routing."""
        skill_dir = skills_dir / "test-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: "Test skill description for tool routing"
---
# Test Skill
""")

        entry_point = skill_dir / "test_skill.py"
        entry_point.write_text("# Entry point")

        loader = SkillLoader(str(skills_dir))
        loader.load_skills()
        schemas = loader.get_tool_schemas()

        # Verify schema structure
        schema = schemas[0]
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        assert schema["parameters"]["type"] == "object"
        assert "properties" in schema["parameters"]
        assert "required" in schema["parameters"]
