"""Unified scenario model (§5.2).

``mode`` decides which keys are legal. Unknown keys are an error. This module
loads and validates. Assertion keys are checked here (§5.3.1); nothing is run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from monkeybot.core.llm.provider import Message
from regression.schema.assertions import validate_assertions
from regression.schema.events import ScriptEvent
from regression.schema.steps import Step

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO_SUFFIXES = {".yaml", ".yml"}

Mode = Literal["scripted", "replay", "live"]
DriverName = Literal["loop", "gateway", "realtime"]
ExecutorName = Literal["recording", "real"]

HOOK_BUNDLES: frozenset[str] = frozenset(
    {
        "verification_gate",
        "evidence_guard",
        "secret_scanner",
        "verifier",
        "git_context",
        "read_ledger_compaction",
        "memory",
    }
)

_COMMON: frozenset[str] = frozenset(
    {
        "id",
        "case",
        "description",
        "tags",
        "regression_of",
        "mode",
        "messages",
        "sessions",
        "session_pause_sec",
        "workspace_fixture",
        "workspace_overlays",
        "config",
        "assertions",
    }
)
_SCRIPTED_AND_REPLAY: frozenset[str] = frozenset(
    {
        "driver",
        "invariants_skip",
        "executor",
        "hooks",
        "policy",
        "memory",
        "history",
        "steps",
    }
)
_LEGAL_KEYS: dict[Mode, frozenset[str]] = {
    "scripted": _COMMON | _SCRIPTED_AND_REPLAY | {"script_partial", "script", "tool_results"},
    "replay": _COMMON | _SCRIPTED_AND_REPLAY | {"cassette"},
    "live": _COMMON | {"trials"},
}


class InvariantSkip(BaseModel):
    """One global-invariant opt-out. ``reason`` is required and must be non-blank."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("name", "reason")
    @classmethod
    def _nonblank(cls, value: str, info: ValidationInfo) -> str:
        text = value.strip()
        if text:
            return text
        if info.field_name == "reason":
            raise ValueError("reason is required")
        raise ValueError("must not be blank")


class ScenarioConfig(BaseModel):
    """Overrides merged over the base agent config, plus ``env``."""

    model_config = ConfigDict(extra="allow")

    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env", mode="before")
    @classmethod
    def _env_strings(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("env must be a mapping")
        parsed: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("env keys must be strings")
            if not isinstance(item, str):
                raise ValueError(f"env[{key}] must be a string")
            parsed[key] = item
        return parsed


class PolicyFixtures(BaseModel):
    """Fixture names for the three policy files."""

    model_config = ConfigDict(extra="forbid")

    command_allowlist: str | None = None
    permissions: str | None = None
    grants: str | None = None


class PalaceDrawer(BaseModel):
    """One seed drawer for ``InMemoryPalace``."""

    model_config = ConfigDict(extra="forbid")

    drawer_id: str = Field(min_length=1)
    content: str
    wing: str = "main"
    room: str = "conversation"
    filed_at: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MemorySeed(BaseModel):
    """``index`` lines, ``palace`` drawers, or both."""

    model_config = ConfigDict(extra="forbid")

    index: list[str] = Field(default_factory=list)
    palace: list[PalaceDrawer] = Field(default_factory=list)


class Scenario(BaseModel):
    """One regression scenario. Construct through :func:`load_scenario` so ``id`` matches the path."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str = Field(min_length=1)
    case: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    regression_of: str | None = None
    mode: Mode

    driver: DriverName | None = None
    messages: list[str] = Field(default_factory=list)
    sessions: list[list[str]] | None = None
    session_pause_sec: float | None = Field(default=None, ge=0)
    script_partial: bool | None = None
    invariants_skip: list[InvariantSkip] = Field(default_factory=list)
    script: list[list[ScriptEvent]] | None = None
    cassette: str | None = None
    executor: ExecutorName | None = None
    tool_results: dict[str, dict[str, Any]] | None = None
    workspace_fixture: str | None = None
    workspace_overlays: list[str] = Field(default_factory=list)
    config: ScenarioConfig | None = None
    hooks: list[str] = Field(default_factory=list)
    policy: PolicyFixtures | None = None
    memory: MemorySeed | None = None
    history: list[Message] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    assertions: dict[str, Any] | list[Any] | None = None
    trials: int | None = Field(default=None, ge=1)

    @field_validator("case")
    @classmethod
    def _case_required(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("case is required")
        return text

    @field_validator("cassette", "workspace_fixture")
    @classmethod
    def _nonempty_path(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("path must be non-empty")
        return value

    @field_validator("hooks")
    @classmethod
    def _known_hooks(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in HOOK_BUNDLES]
        if unknown:
            joined = ", ".join(unknown)
            raise ValueError(f"unknown hook bundles: {joined}")
        return value

    @field_validator("history", mode="before")
    @classmethod
    def _parse_history(cls, value: object) -> list[Message]:
        if not isinstance(value, list):
            raise ValueError("history must be a list of messages")
        parsed: list[Message] = []
        for index, item in enumerate(value):
            if isinstance(item, Message):
                parsed.append(item)
                continue
            if not isinstance(item, dict):
                raise ValueError(f"history[{index}] must be a mapping")
            try:
                parsed.append(Message.from_dict(item))
            except ValueError as exc:
                raise ValueError(f"history[{index}]: {exc}") from exc
        return parsed

    @model_validator(mode="after")
    def _mode_rules(self) -> Self:
        illegal = sorted(self.model_fields_set - _LEGAL_KEYS[self.mode])
        if illegal:
            joined = ", ".join(illegal)
            raise ValueError(f"keys not legal for mode {self.mode!r}: {joined}")
        if "tool_results" in self.model_fields_set:
            executor = self.executor if "executor" in self.model_fields_set else "recording"
            if executor != "recording":
                raise ValueError(
                    "tool_results is legal only for scripted mode with executor recording"
                )
        if "messages" in self.model_fields_set and "sessions" in self.model_fields_set:
            raise ValueError("messages and sessions are alternatives; set only one")
        if self.mode in ("scripted", "replay"):
            if "driver" not in self.model_fields_set:
                self.driver = "loop"
            elif self.driver is None:
                raise ValueError("driver must be loop, gateway, or realtime")
            if "executor" not in self.model_fields_set:
                self.executor = "recording"
            elif self.executor is None:
                raise ValueError("executor must be recording or real")
        if self.mode == "scripted":
            if "script_partial" not in self.model_fields_set:
                self.script_partial = False
            elif self.script_partial is None:
                raise ValueError("script_partial must be a boolean")
        validate_assertions(self.assertions, mode=self.mode, driver=self.driver)
        return self


def scenario_id_from_path(path: Path, *, root: Path | None = None) -> str:
    """Derive a scenario id from its file path.

    The id is the path relative to ``root`` (the repository root by default),
    without the ``.yaml`` or ``.yml`` suffix, using ``/`` separators. A file
    outside ``root`` keeps its own path with the suffix removed.
    """
    candidate = Path(path)
    if candidate.suffix.lower() not in _SCENARIO_SUFFIXES:
        raise ValueError(f"scenario path must end in .yaml or .yml, got {candidate.name!r}")
    base = _REPO_ROOT if root is None else Path(root)
    try:
        relative = candidate.resolve().relative_to(base.resolve())
    except ValueError:
        relative = candidate
    scenario_id = relative.with_suffix("").as_posix()
    if not scenario_id or scenario_id.endswith("/"):
        raise ValueError(f"cannot derive a scenario id from {candidate}")
    return scenario_id


def load_scenario(path: Path, *, root: Path | None = None) -> Scenario:
    """Load one scenario YAML file. The YAML ``id`` must match the path when present."""
    source = Path(path)
    derived = scenario_id_from_path(source, root=root)
    loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"scenario root must be a mapping: {source}")
    data: dict[str, Any] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise ValueError(f"scenario keys must be strings: {source}")
        data[key] = value
    if "id" in data and data["id"] != derived:
        raise ValueError(f"id {data['id']!r} does not match path id {derived!r} ({source})")
    data["id"] = derived
    return Scenario.model_validate(data)


__all__ = [
    "HOOK_BUNDLES",
    "DriverName",
    "ExecutorName",
    "InvariantSkip",
    "MemorySeed",
    "Mode",
    "PalaceDrawer",
    "PolicyFixtures",
    "Scenario",
    "ScenarioConfig",
    "load_scenario",
    "scenario_id_from_path",
]
