"""Identity loader: reads the SOUL/IDENTITY/USER/INDEX/MEMORY/HEARTBEAT/RULES files.

These files compose the Layer-1 of the three-layer prompt (immutable per session).
RULES.md additionally feeds the RulesEnforcementMW for hard vetoes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .errors import HarnessConfigError
from .specs import IdentitySpec


@dataclass
class LoadedIdentity:
    soul: str = ""
    rules: str = ""
    identity: str = ""
    user: str = ""
    index: str = ""
    memory: str = ""
    heartbeat: str = ""
    source_paths: dict[str, str] = field(default_factory=dict)

    def system_prompt_block(self) -> str:
        """Compose identity files into a deterministic block for the system prompt."""
        sections: list[str] = []
        for label, body in (
            ("SOUL", self.soul),
            ("IDENTITY", self.identity),
            ("USER", self.user),
            ("INDEX", self.index),
            ("RULES", self.rules),
            ("MEMORY", self.memory),
            ("HEARTBEAT", self.heartbeat),
        ):
            if body.strip():
                sections.append(f"# === {label} ===\n{body.strip()}")
        return "\n\n".join(sections)


class IdentityLoader:
    def __init__(self, spec: IdentitySpec) -> None:
        self.spec = spec

    def load(self) -> LoadedIdentity:
        base = Path(self.spec.dir)
        files = {
            "soul": self.spec.soul_file,
            "rules": self.spec.rules_file,
            "identity": self.spec.identity_file,
            "user": self.spec.user_file,
            "index": self.spec.index_file,
            "memory": self.spec.memory_file,
            "heartbeat": self.spec.heartbeat_file,
        }
        loaded = LoadedIdentity()
        for attr, fname in files.items():
            path = base / fname
            if path.exists():
                setattr(loaded, attr, path.read_text())
                loaded.source_paths[attr] = str(path)
        if self.spec.enforce_rules and not loaded.rules:
            raise HarnessConfigError(
                f"RULES.md is required at {base / self.spec.rules_file} when enforce_rules=True"
            )
        return loaded
