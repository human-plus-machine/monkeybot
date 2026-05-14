"""Safety inspector factory — builds inspector chain from config.yaml dict."""
from __future__ import annotations

from typing import Any

from monkeybot.core.inspector import CommandTierInspector, RulesInspector, ToolInspector


def load_inspectors(config: dict[str, Any]) -> list[ToolInspector]:
    """Build inspector chain from parsed config.yaml dict.

    Args:
        config: Full bot config dict (or {} for dev mode).

    Returns:
        Ordered list of ToolInspector instances. Empty list = allow all.
        Order: [CommandTierInspector, RulesInspector] when both present.

    Raises:
        Nothing. Missing/malformed keys are treated as absent.
    """
    safety = config.get("safety") or {}
    if not isinstance(safety, dict):
        return []
    inspectors: list[ToolInspector] = []
    tiers = safety.get("command_tiers")
    if isinstance(tiers, dict):
        inspectors.append(CommandTierInspector(tiers))
    patterns = safety.get("denied_patterns")
    if isinstance(patterns, list):
        inspectors.append(RulesInspector(denied_patterns=patterns))
    return inspectors
