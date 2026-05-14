from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class SkillRef:
    """Reference to an available skill loaded from the skills directory.

    Attributes:
        name: Directory name under skills_path.
        description: First non-heading line of SKILL.md, max 120 chars.
        path: Absolute path to SKILL.md.
    """

    name: str
    description: str
    path: str


@dataclass(frozen=True)
class TurnContext:
    """Immutable context passed to the provider and inspectors for each turn.

    Attributes:
        agent_md: Raw content of the AGENT.md file.
        memory_index: One-line summaries from all memory documents.
        skills: Available skills discovered from the skills directory.
        memory_path: Absolute memory directory path (for {MEMORY_PATH} substitution).
        skills_path: Absolute skills directory path.
        user_id: Optional identifier of the user initiating the turn.
        parent_run_id: Optional parent run identifier for tracing.
        run_id: Optional run identifier for this turn.
    """

    agent_md: str
    memory_index: list[str]
    skills: list[SkillRef]
    memory_path: str
    skills_path: str
    user_id: str | None = None
    parent_run_id: str | None = None
    run_id: str | None = None

    def build_system_prompt(self) -> str:
        """Build the complete system prompt from agent_md, memory index, and skills.

        Returns:
            A single string combining all context sections.
        """
        agent_md_resolved = self.agent_md.replace("{MEMORY_PATH}", self.memory_path)
        parts = [agent_md_resolved]
        if self.memory_index:
            parts.append(
                "\n## Memory Index\n" + "\n".join(f"- {line}" for line in self.memory_index)
            )
        if self.skills:
            skill_lines = "\n".join(
                f"- **{s.name}**: {s.description}" for s in self.skills
            )
            parts.append(
                "\n## Available Skills\n"
                + skill_lines
                + "\nTo use a skill, call list_skills() then read_file() on the SKILL.md path"
                " to get full instructions."
            )
        return "\n".join(parts)


def load_turn_context(
    agent_md_path: str,
    memory_path: str,
    skills_path: str,
    user_id: str | None = None,
    parent_run_id: str | None = None,
    run_id: str | None = None,
) -> TurnContext:
    """Load a TurnContext from the filesystem.

    Args:
        agent_md_path: Path to the AGENT.md file (must exist).
        memory_path: Directory containing memory markdown files (may be missing).
        skills_path: Directory containing skill subdirectories (may be missing).
        user_id: Optional user identifier.
        parent_run_id: Optional parent run identifier.
        run_id: Optional run identifier.

    Returns:
        A populated TurnContext.

    Raises:
        FileNotFoundError: If agent_md_path does not exist.
    """
    agent_md = Path(agent_md_path).read_text()
    memory_index = _build_memory_index(memory_path)
    skills = _scan_skills(skills_path)
    memory_path_abs = str(Path(memory_path).expanduser().resolve())
    skills_path_abs = str(Path(skills_path).expanduser().resolve())
    return TurnContext(
        agent_md=agent_md,
        memory_index=memory_index,
        skills=skills,
        memory_path=memory_path_abs,
        skills_path=skills_path_abs,
        user_id=user_id,
        parent_run_id=parent_run_id,
        run_id=run_id,
    )


def _build_memory_index(memory_path: str) -> list[str]:
    """Scan memory directory and build one-line index entries.

    Args:
        memory_path: Directory to scan for .md files.

    Returns:
        Sorted list of "{stem}: {first_line}" strings. Empty if path missing.
    """
    try:
        files = sorted(Path(memory_path).glob("**/*.md"))
    except OSError:
        return []

    entries: list[str] = []
    for f in files:
        try:
            lines = f.read_text().splitlines()
            first_line = lines[0].lstrip("#").strip()
            entries.append(f"{f.stem}: {first_line}")
        except (OSError, IndexError):
            continue
    return entries


def _scan_skills(skills_path: str) -> list[SkillRef]:
    """Scan skills directory and build SkillRef entries.

    Args:
        skills_path: Directory to scan for subdirectories containing SKILL.md.

    Returns:
        Sorted list of SkillRef objects. Empty if path missing.
    """
    try:
        skill_files = sorted(Path(skills_path).glob("*/SKILL.md"))
    except OSError:
        return []

    skills: list[SkillRef] = []
    for skill_md in skill_files:
        name = skill_md.parent.name
        description = _first_content_line(skill_md)
        skills.append(SkillRef(name=name, description=description, path=str(skill_md)))
    return skills


def _first_content_line(path: Path) -> str:
    """Return the first non-empty, non-heading line of a file, max 120 chars.

    Args:
        path: Path to the markdown file.

    Returns:
        The first content line, or empty string if none found.
    """
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped[:120]
    except OSError:
        pass
    return ""
