"""Per-tool output shaping budgets (command_allowlist.yaml ``tool_output`` section)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from monkeybot.core.config.snapshot import current_env
from monkeybot.core.tools.inspector import CommandTierConfigError

ContentTypeHint = Literal["json", "logs", "code", "prose", "auto"]
_SUPPORTED_CONTENT_TYPES = frozenset({"json", "logs", "code", "prose", "auto"})
_MIN_OUTPUT_LINES = 5


@dataclass(frozen=True)
class ToolOutputBudget:
    """Operator-configured caps and shaper hints for one tool name."""

    content_type: ContentTypeHint | None = None
    max_output_lines: int | None = None
    max_array_items: int | None = None
    keep_patterns: tuple[str, ...] = ()
    collapse_repeated: bool = False


# Sensible defaults when YAML omits ``tool_output`` entries.
_MCP_DEFAULT_TOOL_BUDGET = ToolOutputBudget(
    content_type="auto",
    max_output_lines=120,
    max_array_items=40,
)

_known_mcp_tools: frozenset[str] = frozenset()


_BUILTIN_TOOL_BUDGETS: dict[str, ToolOutputBudget] = {
    "run_command": ToolOutputBudget(
        content_type="logs",
        max_output_lines=400,
        collapse_repeated=True,
        keep_patterns=(
            r"(?i)\berror\b",
            r"(?i)\bfatal\b",
            r"(?i)traceback",
            r"(?i)\bexception\b",
        ),
    ),
    "web_search": ToolOutputBudget(
        content_type="json",
        max_array_items=30,
    ),
}


def _resolve_policy_path(path: Path | None) -> Path | None:
    if path is not None:
        return path if path.is_file() else None
    raw = current_env("COMMAND_ALLOWLIST_CONFIG", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_file() else None


def _parse_tool_output_entry(path: Path, tool_name: str, raw: Any) -> ToolOutputBudget:
    if not isinstance(raw, dict):
        raise CommandTierConfigError(path, f"tool_output.{tool_name} must be a mapping")
    content_type: ContentTypeHint | None = None
    ct_raw = raw.get("content_type")
    if ct_raw is not None:
        if not isinstance(ct_raw, str) or ct_raw.strip().lower() not in _SUPPORTED_CONTENT_TYPES:
            raise CommandTierConfigError(
                path,
                f"tool_output.{tool_name}.content_type must be one of "
                f"{sorted(_SUPPORTED_CONTENT_TYPES)}",
            )
        content_type = ct_raw.strip().lower()  # type: ignore[assignment]

    max_output_lines: int | None = None
    if "max_output_lines" in raw:
        val = raw["max_output_lines"]
        if not isinstance(val, int) or val < 1:
            raise CommandTierConfigError(
                path, f"tool_output.{tool_name}.max_output_lines must be a positive integer"
            )
        max_output_lines = val

    max_array_items: int | None = None
    if "max_array_items" in raw:
        val = raw["max_array_items"]
        if not isinstance(val, int) or val < 1:
            raise CommandTierConfigError(
                path, f"tool_output.{tool_name}.max_array_items must be a positive integer"
            )
        max_array_items = val

    keep_patterns: tuple[str, ...] = ()
    kp_raw = raw.get("keep_patterns")
    if kp_raw is not None:
        if not isinstance(kp_raw, list):
            raise CommandTierConfigError(
                path, f"tool_output.{tool_name}.keep_patterns must be a list of regex strings"
            )
        compiled: list[str] = []
        for i, item in enumerate(kp_raw):
            if not isinstance(item, str) or not item.strip():
                raise CommandTierConfigError(
                    path, f"tool_output.{tool_name}.keep_patterns[{i}] must be a non-empty string"
                )
            try:
                re.compile(item)
            except re.error as exc:
                raise CommandTierConfigError(
                    path,
                    f"tool_output.{tool_name}.keep_patterns[{i}]: invalid regex: {exc}",
                ) from exc
            compiled.append(item)
        keep_patterns = tuple(compiled)

    collapse_repeated = bool(raw.get("collapse_repeated", False))
    return ToolOutputBudget(
        content_type=content_type,
        max_output_lines=max_output_lines,
        max_array_items=max_array_items,
        keep_patterns=keep_patterns,
        collapse_repeated=collapse_repeated,
    )


def parse_tool_output_section(path: Path, data: dict[str, Any]) -> dict[str, ToolOutputBudget]:
    """Parse optional ``tool_output`` mapping from command allowlist YAML."""
    raw = data.get("tool_output")
    if raw is None:
        return dict(_BUILTIN_TOOL_BUDGETS)
    if not isinstance(raw, dict):
        raise CommandTierConfigError(path, "'tool_output' must be a mapping of tool names")

    merged = dict(_BUILTIN_TOOL_BUDGETS)
    for tool_name, entry in raw.items():
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise CommandTierConfigError(path, "tool_output keys must be non-empty tool names")
        parsed = _parse_tool_output_entry(path, tool_name.strip(), entry)
        base = merged.get(tool_name.strip())
        if base is not None:
            merged[tool_name.strip()] = ToolOutputBudget(
                content_type=(
                    parsed.content_type if "content_type" in entry else base.content_type
                ),
                max_output_lines=(
                    parsed.max_output_lines
                    if "max_output_lines" in entry
                    else base.max_output_lines
                ),
                max_array_items=(
                    parsed.max_array_items if "max_array_items" in entry else base.max_array_items
                ),
                keep_patterns=(
                    parsed.keep_patterns if "keep_patterns" in entry else base.keep_patterns
                ),
                collapse_repeated=(
                    parsed.collapse_repeated
                    if "collapse_repeated" in entry
                    else base.collapse_repeated
                ),
            )
        else:
            merged[tool_name.strip()] = parsed
    return merged


def load_tool_output_policies(path: Path | None = None) -> dict[str, ToolOutputBudget]:
    """Load merged built-in + YAML per-tool output budgets."""
    resolved = _resolve_policy_path(path)
    if resolved is None:
        return dict(_BUILTIN_TOOL_BUDGETS)
    try:
        data = yaml.safe_load(resolved.read_bytes())
    except yaml.YAMLError as exc:
        raise CommandTierConfigError(resolved, f"invalid YAML: {exc}") from exc
    if data is None:
        return dict(_BUILTIN_TOOL_BUDGETS)
    if not isinstance(data, dict):
        raise CommandTierConfigError(resolved, "root must be a mapping")
    return parse_tool_output_section(resolved, data)


@lru_cache(maxsize=1)
def cached_tool_output_policies() -> dict[str, ToolOutputBudget]:
    """Process-wide cache of tool output policies (invalidated only on restart)."""
    return load_tool_output_policies()


def register_mcp_tool_names(names: Iterable[str]) -> None:
    """Record prefixed MCP tool names for output-budget resolution (updated on MCP connect)."""
    global _known_mcp_tools
    added = frozenset(n for n in names if n)
    if not added:
        return
    _known_mcp_tools = _known_mcp_tools | added


def unregister_mcp_server_tools(server_name: str) -> None:
    """Drop every ``server__*`` tool registered for ``server_name`` (MCP disconnect)."""
    global _known_mcp_tools
    if not server_name:
        return
    prefix = f"{server_name}__"
    _known_mcp_tools = frozenset(n for n in _known_mcp_tools if not n.startswith(prefix))


def reset_mcp_tool_registry_for_tests() -> None:
    """Clear the MCP tool registry (tests only)."""
    global _known_mcp_tools
    _known_mcp_tools = frozenset()


def resolve_tool_budget(tool_name: str) -> ToolOutputBudget | None:
    """Return configured budget for ``tool_name``, or MCP default for registered MCP tools."""
    merged = cached_tool_output_policies()
    if tool_name in merged:
        return merged[tool_name]
    if tool_name in _known_mcp_tools:
        return merged.get("*") or _MCP_DEFAULT_TOOL_BUDGET
    return None


def validate_tool_output_budgets(
    policies: dict[str, ToolOutputBudget],
) -> list[str]:
    """Return human-readable warnings for implausibly tight operator caps."""
    warnings: list[str] = []
    for name, budget in policies.items():
        if budget.max_output_lines is not None and budget.max_output_lines < _MIN_OUTPUT_LINES:
            warnings.append(
                f"tool_output.{name}.max_output_lines={budget.max_output_lines} is very small "
                f"(<{_MIN_OUTPUT_LINES}); tool output may lose critical signal"
            )
        if budget.max_array_items is not None and budget.max_array_items < 3:
            warnings.append(
                f"tool_output.{name}.max_array_items={budget.max_array_items} is very small; "
                "search/API results may be unusable"
            )
    return warnings


def reset_tool_output_policy_cache_for_tests() -> None:
    cached_tool_output_policies.cache_clear()
    reset_mcp_tool_registry_for_tests()


def invalidate_config_caches() -> None:
    """Drop process-wide caches derived from YAML / context-window config.

    Called from gateway config reload. Does not clear the MCP tool-name registry
    — that tracks live connections, not file contents.
    """
    cached_tool_output_policies.cache_clear()
    from monkeybot.core.tools.core_tool_executor import workspace_settings_from_config
    from monkeybot.core.tools.spill_inventory import spill_budgets_from_window

    workspace_settings_from_config.cache_clear()
    spill_budgets_from_window.cache_clear()


__all__ = [
    "ToolOutputBudget",
    "invalidate_config_caches",
    "load_tool_output_policies",
    "parse_tool_output_section",
    "register_mcp_tool_names",
    "resolve_tool_budget",
    "reset_mcp_tool_registry_for_tests",
    "reset_tool_output_policy_cache_for_tests",
    "unregister_mcp_server_tools",
    "validate_tool_output_budgets",
]
