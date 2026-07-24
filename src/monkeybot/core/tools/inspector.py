"""Tool inspectors: ``run_command`` policy (allowlists + optional deny-regex YAML)."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from monkeybot.core.context import TurnContext
from monkeybot.core.types.interfaces import MonkeybotError
from monkeybot.core.tools.terminal import ALLOWED_COMMANDS, ALLOWED_PATHS

# Default deny-regex lines when YAML omits ``deny_patterns``. Blocks package installs
# via common verbs (pip, uv, npm, apt, brew, etc.) while leaving script execution allowed.
DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    r"pip3?\s+install",
    r"python3?\s+-m\s+pip\s+install",
    r"(^|\s)uv\s+(add|remove|sync|pip\s+install|pip\s+sync)",
    r"(^|\s)poetry\s+(add|install)",
    r"(^|\s)conda\s+install",
    r"(^|\s)(npm|pnpm|yarn)\s+(install|add|i)(\s|$)",
    r"(^|\s)(apt|apt-get)\s+install",
    r"(^|\s)brew\s+install",
)


class CommandTierConfigError(MonkeybotError):
    """Invalid or unloadable run_command policy file."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class Decision:
    """Allow, deny, or confirm outcome from a tool inspector.

    ``confirm`` requires an SSE session so the user can approve via tool-confirmations.
    """

    kind: Literal["allow", "deny", "confirm"]
    message: str | None = None


@dataclass(frozen=True)
class InspectorToolCall:
    """Normalized tool call at the inspection boundary (Story 6 maps provider calls here)."""

    call_id: str
    name: str
    args: dict[str, object]


class ToolInspector(Protocol):
    """Pre-flight gate for tool execution."""

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        """Return allow or deny before executing the tool."""
        ...


@dataclass(frozen=True)
class CommandTierPolicy:
    """Parsed ``command_allowlist.yaml``: execution allowlists + optional deny-regex lines."""

    allowed_commands: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...]
    deny_patterns: tuple[str, ...]


def _optional_nonempty_str_list(path: Path, data: dict[str, Any], key: str) -> list[str] | None:
    if key not in data:
        return None
    val = data[key]
    if not isinstance(val, list) or not val:
        raise CommandTierConfigError(
            path, f"'{key}' must be a non-empty list of strings when present"
        )
    out: list[str] = []
    for i, item in enumerate(val):
        if not isinstance(item, str) or not item.strip():
            raise CommandTierConfigError(
                path, f"'{key}[{i}]' must be a non-empty string"
            )
        out.append(item)
    return out


def coerce_run_command_argv(argv_raw: object) -> list[str] | None:
    """Normalize ``argv`` to a non-empty ``list[str]``.

    Accepts a real list or a JSON-encoded list string (common LLM tool-arg quirk).
    Returns ``None`` when ``argv`` is absent or empty.
    Raises ``ValueError`` when ``argv`` is present but not coercible to a list.
    """
    if argv_raw is None:
        return None

    if isinstance(argv_raw, list):
        if not argv_raw:
            return None
        return [str(x) for x in argv_raw]

    if isinstance(argv_raw, str):
        stripped = argv_raw.strip()
        if not stripped:
            return None
        if not stripped.startswith("["):
            raise ValueError(
                "argv must be an array (or JSON array string), got string"
            )
        try:
            parsed: object = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise ValueError(
                "argv must be an array (or JSON array string), "
                f"got string that is not valid JSON: {e}"
            ) from e
        if isinstance(parsed, list) and parsed:
            return [str(x) for x in parsed]
        kind = type(parsed).__name__
        raise ValueError(
            "argv must be a non-empty array (or JSON array string), "
            f"got JSON {kind}"
        )

    raise ValueError(
        "argv must be an array (or JSON array string), "
        f"got {type(argv_raw).__name__}"
    )


def norm_run_command_line(args: dict[str, object]) -> str | None:
    """Build one line for deny-regex checks (mirrors ``run_command`` argv shapes).

    Raises ``ValueError`` when ``argv`` is present but not coercible (see
    ``coerce_run_command_argv``).
    """
    argv = coerce_run_command_argv(args.get("argv"))
    if argv:
        return shlex.join(argv)

    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        extra = args.get("args")
        if extra is None:
            extra = args.get("arguments")
        if isinstance(extra, list) and extra:
            parts = [cmd.strip()] + [str(x) for x in extra]
            return shlex.join(parts)
        parts = shlex.split(cmd.strip(), posix=True)
        if len(parts) > 1:
            return shlex.join(parts)
        return " ".join(cmd.strip().split())

    shell = args.get("shell") or args.get("script")
    if isinstance(shell, str) and shell.strip():
        return " ".join(shell.strip().split())

    return None


def load_command_tier_policy(path: Path) -> CommandTierPolicy:
    """Load ``run_command`` allowlists and optional ``deny_patterns`` from policy YAML."""
    raw_bytes = path.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as e:
        raise CommandTierConfigError(path, f"invalid YAML: {e}") from e

    if data is None:
        raise CommandTierConfigError(path, "file is empty")
    if not isinstance(data, dict):
        raise CommandTierConfigError(path, "root must be a mapping")

    for legacy_key in ("tier_order", "default"):
        if legacy_key in data:
            raise CommandTierConfigError(
                path,
                f"obsolete key {legacy_key!r}: tier-based policy was removed; use "
                "'allowed_commands', 'allowed_path_prefixes', and optional 'deny_patterns'",
            )

    ac_raw = _optional_nonempty_str_list(path, data, "allowed_commands")
    ap_raw = _optional_nonempty_str_list(path, data, "allowed_path_prefixes")

    deny_raw = data.get("deny_patterns")
    if deny_raw is None:
        deny_patterns = DEFAULT_DENY_PATTERNS
    elif isinstance(deny_raw, list):
        for i, item in enumerate(deny_raw):
            if not isinstance(item, str) or not item.strip():
                raise CommandTierConfigError(
                    path, f"'deny_patterns[{i}]' must be a non-empty string"
                )
        deny_patterns = tuple(str(x) for x in deny_raw)
    else:
        raise CommandTierConfigError(
            path, "'deny_patterns' must be a list of strings when present"
        )

    allowed_commands = tuple(ac_raw) if ac_raw is not None else tuple(ALLOWED_COMMANDS)
    allowed_path_prefixes = tuple(ap_raw) if ap_raw is not None else tuple(ALLOWED_PATHS)

    return CommandTierPolicy(
        allowed_commands=allowed_commands,
        allowed_path_prefixes=allowed_path_prefixes,
        deny_patterns=deny_patterns,
    )


class CommandTierInspector:
    """YAML policy: optional deny-regex preflight; execution allowlists are on the policy object."""

    def __init__(self, tier_config_path: Path) -> None:
        self._path = tier_config_path
        self._policy = load_command_tier_policy(tier_config_path)
        compiled: list[re.Pattern[str]] = []
        for i, pat in enumerate(self._policy.deny_patterns):
            try:
                compiled.append(re.compile(pat, flags=re.IGNORECASE))
            except re.error as e:
                raise CommandTierConfigError(
                    tier_config_path, f"deny_patterns[{i}]: invalid regex: {e}"
                ) from e
        self._deny_regexes = tuple(compiled)

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        return self._policy.allowed_commands

    @property
    def allowed_path_prefixes(self) -> tuple[str, ...]:
        return self._policy.allowed_path_prefixes

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        """Deny if any ``deny_patterns`` matches the normalized invocation; else allow."""
        del ctx
        if call.name != "run_command":
            return Decision(kind="allow")

        try:
            cmd_norm = norm_run_command_line(call.args)
        except ValueError as e:
            return Decision(kind="deny", message=str(e))
        if not cmd_norm:
            return Decision(
                kind="deny",
                message="run_command policy check needs argv, command, or shell/script",
            )

        for rx in self._deny_regexes:
            if rx.search(cmd_norm):
                return Decision(kind="deny", message="denied by run_command deny_patterns policy")

        return Decision(kind="allow")


class RulesInspector:
    """Blocks tool calls whose serialized arguments contain any denied substring."""

    def __init__(self, denied_patterns: list[str]) -> None:
        self.denied_patterns = denied_patterns

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        del ctx
        args_str = str(call.args)
        for pattern in self.denied_patterns:
            if pattern in args_str:
                return Decision(kind="deny", message=f"Blocked by rules: {pattern}")
        return Decision(kind="allow")
