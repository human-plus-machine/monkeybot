"""
Skill discovery and loading from filesystem.

This module provides the SkillLoader class which discovers skills from the
./skills/ directory by parsing SKILL.md files with YAML frontmatter.

Example:
    >>> loader = SkillLoader()
    >>> skills = loader.load_skills()
    >>> print(list(skills.keys()))
    ['file-ops', 'memory']
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import yaml

logger = logging.getLogger(__name__)


class SkillLoader:
    """
    Discover and load skills from filesystem.

    Skills are discovered by scanning the skills directory for subdirectories
    containing SKILL.md files with YAML frontmatter. Each skill must have:
    - A SKILL.md file with YAML frontmatter containing 'name' and 'description'
    - A Python entry point file ({skill_name}.py)

    Example skill directory structure:
        skills/
            file-ops/
                SKILL.md
                file_ops.py
            memory/
                SKILL.md
                memory.py

    Attributes:
        skills_dir: Path to skills directory
        skills: Dictionary mapping skill names to metadata

    Example:
        >>> loader = SkillLoader("./skills")
        >>> skills = loader.load_skills()
        >>> print(skills["file-ops"]["description"])
        'File operations (read, write, list)'
    """

    def __init__(self, skills_dir: str = "./skills"):
        """
        Initialize skill loader.

        Args:
            skills_dir: Path to skills directory (default: ./skills)
        """
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, dict[str, Any]] = {}

    def load_skills(self) -> dict[str, dict[str, Any]]:
        """
        Discover all skills in skills directory.

        Scans the skills directory for subdirectories containing SKILL.md files,
        parses the YAML frontmatter, and validates the entry point exists.

        Returns:
            Dictionary mapping skill_name to skill metadata:
            {
                "skill-name": {
                    "metadata": {...},           # YAML frontmatter
                    "entry_point": "/path/to/skill.py",
                    "description": "Skill description"
                }
            }

        Notes:
            - Duplicate skill names: First one wins, others logged as warnings
            - Missing SKILL.md: Directory skipped with warning
            - Invalid YAML: Skill skipped with error log
            - Missing entry point: Metadata loaded but warning logged
        """
        logger.info(f"Loading skills from {self.skills_dir}")

        if not self.skills_dir.exists():
            logger.warning(f"Skills directory not found: {self.skills_dir}")
            return {}

        for skill_path in self.skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                logger.debug(
                    f"Skipping {skill_path.name} - no SKILL.md found",
                    extra={"component": "skill_loader", "path": str(skill_path)},
                )
                continue

            # Parse SKILL.md
            skill_metadata = self._parse_skill_md(skill_md)
            if not skill_metadata:
                continue

            skill_name = skill_metadata.get("name")
            if not skill_name:
                logger.error(
                    f"Skill {skill_path.name} missing 'name' in metadata",
                    extra={"component": "skill_loader", "path": str(skill_md)},
                )
                continue

            # Check for duplicate
            if skill_name in self.skills:
                logger.warning(
                    f"Duplicate skill name: {skill_name}. Using first occurrence.",
                    extra={"component": "skill_loader", "skill": skill_name},
                )
                continue

            # Find Python entry point
            entry_point = skill_path / f"{skill_name.replace('-', '_')}.py"
            if not entry_point.exists():
                logger.warning(
                    f"Skill {skill_name} missing entry point: {entry_point}",
                    extra={"component": "skill_loader", "skill": skill_name},
                )
                # Continue loading metadata even without entry point

            self.skills[skill_name] = {
                "metadata": skill_metadata,
                "entry_point": str(entry_point),
                "description": skill_metadata.get("description", ""),
            }

            logger.info(
                f"Loaded skill: {skill_name}",
                extra={"component": "skill_loader", "skill": skill_name},
            )

        logger.info(
            f"Loaded {len(self.skills)} skills",
            extra={"component": "skill_loader", "count": len(self.skills)},
        )

        return self.skills

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """
        Convert loaded skills to JSON tool schema dictionaries.

        Each skill becomes a tool definition (name, description, parameters)
        suitable for LLM function-calling or routing layers.

        Returns:
            List of tool schema dictionaries, for example:
            [
                {
                    "name": "generate-post",
                    "description": "Create social media content for any platform...",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                },
                ...
            ]

        Notes:
            - Description is extracted from SKILL.md frontmatter
            - Parameters are auto-generated (empty for MVP)
            - Required fields list is empty (Gemini extracts from description)

        Example:
            >>> loader = SkillLoader()
            >>> loader.load_skills()
            >>> schemas = loader.get_tool_schemas()
            >>> print(schemas[0]["name"])
            'generate-post'
            >>> print(schemas[0]["description"][:50])
            'Create social media content for any platform...'
        """
        tool_schemas = []

        for skill_name, skill_data in self.skills.items():
            description = skill_data.get("description", "")

            # Validation: Ensure description is meaningful
            if len(description) < 20:
                logger.warning(
                    f"Skill '{skill_name}' has short description ({len(description)} chars). "
                    "May affect routing quality.",
                    extra={"component": "skill_loader", "skill": skill_name},
                )

            schema = {
                "name": skill_name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {},  # Auto-inferred by Gemini from description
                    "required": [],
                },
            }

            tool_schemas.append(schema)

        logger.info(
            f"Generated {len(tool_schemas)} tool schemas from skills",
            extra={"component": "skill_loader", "count": len(tool_schemas)},
        )

        return tool_schemas

    def _parse_skill_md(self, skill_md_path: Path) -> Optional[dict[str, Any]]:
        """
        Parse SKILL.md file and extract YAML frontmatter.

        SKILL.md format:
            ---
            name: skill-name
            description: "Description"
            metadata:
              monkeybot:
                requires:
                  bins: ["cat", "ls"]
            ---

            # Skill Documentation
            ...

        Args:
            skill_md_path: Path to SKILL.md file

        Returns:
            Parsed metadata dict or None if parsing fails

        Notes:
            - Uses yaml.safe_load() to prevent code execution
            - Malformed YAML results in None (logged as error)
            - Missing frontmatter results in None (logged as error)
        """
        try:
            with open(skill_md_path, "r") as f:
                content = f.read()

            # Extract YAML frontmatter between --- delimiters
            if not content.startswith("---"):
                logger.error(
                    f"SKILL.md missing frontmatter: {skill_md_path}",
                    extra={"component": "skill_loader", "path": str(skill_md_path)},
                )
                return None

            parts = content.split("---", 2)
            if len(parts) < 3:
                logger.error(
                    f"SKILL.md malformed frontmatter: {skill_md_path}",
                    extra={"component": "skill_loader", "path": str(skill_md_path)},
                )
                return None

            frontmatter = parts[1].strip()
            metadata = yaml.safe_load(frontmatter)

            return cast(dict[str, Any], metadata)

        except yaml.YAMLError as e:
            logger.error(
                f"Failed to parse YAML in {skill_md_path}: {e}",
                extra={"component": "skill_loader", "error": str(e)},
            )
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error parsing {skill_md_path}: {e}",
                extra={"component": "skill_loader", "error": str(e)},
            )
            return None
