"""CommandTierMW — classify shell/tool commands into preapproved / requires_approval / denied."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal

from ..errors import SandboxDenied
from ..specs import CommandTierSpec

Tier = Literal["preapproved", "requires_approval", "denied"]


@dataclass
class CommandTierClassifier:
    spec: CommandTierSpec

    def classify(self, cmd: str) -> Tier:
        """Classify a space-joined command string.

        A pattern matches if fnmatch matches the first N whitespace-normalized tokens
        of ``cmd`` (so ``git push`` matches ``git push origin main``).
        """
        if _any_match(cmd, self.spec.denied):
            return "denied"
        if _any_match(cmd, self.spec.requires_approval):
            return "requires_approval"
        if _any_match(cmd, self.spec.preapproved):
            return "preapproved"
        return "requires_approval"


def _any_match(cmd: str, patterns: list[str]) -> bool:
    norm = " ".join(cmd.split())
    for pat in patterns:
        pat_norm = " ".join(pat.split())
        if fnmatch.fnmatch(norm, pat_norm) or fnmatch.fnmatch(norm, pat_norm + " *"):
            return True
        if norm.startswith(pat_norm):
            return True
    return False


class CommandTierMW:
    name = "CommandTierMW"

    def __init__(self, spec: CommandTierSpec) -> None:
        self.classifier = CommandTierClassifier(spec)

    def classify(self, cmd: str) -> Tier:
        return self.classifier.classify(cmd)

    def enforce(self, cmd: str) -> Tier:
        tier = self.classify(cmd)
        if tier == "denied":
            raise SandboxDenied("command tier=denied", resource=cmd)
        return tier
