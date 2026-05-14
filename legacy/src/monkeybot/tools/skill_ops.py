"""list_skills — skill discovery."""
from __future__ import annotations

from pathlib import Path

TOOL_DEF = {
    "name": "list_skills",
    "description": (
        "List all available skills with their descriptions. "
        "Call this when you need to find a capability. "
        "Then use read_file() on the skill's SKILL.md to get full instructions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Optional keyword to filter skills"},
        },
    },
}


def list_skills(skills_path: str, filter: str | None = None) -> str:
    """List available skills with optional keyword filter."""
    p = Path(skills_path)
    if not p.exists():
        return "No skills directory found."

    skills = []
    for skill_md in sorted(p.glob("*/SKILL.md")):
        name = skill_md.parent.name
        desc = ""
        lines = skill_md.read_text().splitlines()
        for line in lines[1:]:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                desc = stripped[:120]
                break
        if filter and filter.lower() not in name.lower() and filter.lower() not in desc.lower():
            continue
        skills.append(f"- **{name}** ({skill_md}): {desc}")

    if not skills:
        return "No skills found."

    return "Available skills:\n" + "\n".join(skills)
