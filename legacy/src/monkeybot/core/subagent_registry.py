from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from monkeybot.core.subagent_proto import SubagentDefinition

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SubagentRegistry:
    """Registry of named subagents loaded from config.yaml.

    Validates and stores SubagentDefinition instances. Provides resolution
    by name, prompt block generation, and startup path validation.
    """

    def __init__(
        self,
        registry_block: dict[str, Any],
        *,
        bot_skills_path: str,
        bot_model: str,
        global_timeout: int = 300,
    ) -> None:
        """Validate and load all SubagentDefinitions.

        Args:
            registry_block: config["subagents"].get("registry", {})
            bot_skills_path: Fallback skills path from bot config.
            bot_model: Fallback model from bot config.
            global_timeout: Default timeout in seconds if not per-definition.

        Raises:
            ValueError: If any name is invalid or description/script is missing.
        """
        self._definitions: dict[str, SubagentDefinition] = {}
        for name, cfg in registry_block.items():
            if not _NAME_RE.match(name):
                raise ValueError(
                    f"Invalid subagent name: '{name}'. "
                    f"Must match ^[a-z0-9][a-z0-9-]*$"
                )
            if not isinstance(cfg, dict):
                raise ValueError(f"Subagent '{name}' config must be a dict")
            description = cfg.get("description", "")
            if not description or not str(description).strip():
                raise ValueError(f"Subagent '{name}' requires a non-empty description")
            script = cfg.get("script", "")
            if not script:
                raise ValueError(f"Subagent '{name}' requires a script path")
            self._definitions[name] = SubagentDefinition(
                name=name,
                script=str(script),
                description=str(description).strip(),
                skills_path=str(cfg.get("skills_path", bot_skills_path)),
                model=str(cfg.get("model", bot_model)),
                timeout_seconds=int(cfg.get("timeout_seconds", global_timeout)),
            )

    def resolve(self, name: str) -> SubagentDefinition:
        """Return definition for *name*.

        Raises:
            KeyError: With message listing available names if not found.
        """
        if name not in self._definitions:
            available = ", ".join(self._definitions) or "(none)"
            raise KeyError(f"No subagent '{name}'. Available: {available}")
        return self._definitions[name]

    def all_definitions(self) -> list[SubagentDefinition]:
        """Return all definitions in insertion order. Returns a copy."""
        return list(self._definitions.values())

    def to_prompt_block(self) -> str:
        """Return a markdown table of available subagents for system prompt injection.

        Returns empty string if registry is empty.
        """
        if not self._definitions:
            return ""
        rows = "\n".join(
            f"| {d.name} | {d.description} |" for d in self._definitions.values()
        )
        return f"## Available Subagents\n| Name | Description |\n|------|-------------|\n{rows}"

    def validate(self) -> list[str]:
        """Check all script paths exist relative to Path.cwd().

        Returns list of error strings. Empty list means all OK.
        Note: Paths are resolved relative to the current working directory
        at the time validate() is called.
        """
        errors: list[str] = []
        for d in self._definitions.values():
            if not Path(d.script).exists():
                errors.append(f"subagent '{d.name}': script '{d.script}' not found")
        return errors
