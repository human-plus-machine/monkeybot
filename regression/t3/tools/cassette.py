"""T3a trajectory cassette: the §5.5.1 JSON as a Pydantic model, plus replay checks.

A cassette is the provider side of one recorded run: what the model answered on
each ``stream()`` call, plus the drift checker's classifier rows and judge
completions when that run recorded them. The harness side (tools, workspace,
loop) runs for real on replay. Staleness has two levels:

* **Strict.** A tool the cassette calls is gone, or an argument key it passes is no
  longer in that tool's input schema. That is ``cassette_stale: tool <name> changed``
  and the replay fails. Re-record.
* **Digest.** ``tool_schema_digest`` or ``prompt_digest`` differs. The model might
  have chosen differently, so that is a warning only.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from regression.schema.events import ScriptEvent, ToolCallEvent

SCHEMA_VERSION = 1
RECORD_ENV = "MONKEYBOT_RECORD_CASSETTES"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# The harness block up to its first per-run section. Everything after
# ``### Runtime paths`` carries workspace paths, memory state, MCP and skills.
_PREFIX_START = "## monkeybot harness (fixed)"
_PREFIX_END = "### Runtime paths"
_DATE_MARK = "\n## Current date"


class CassetteStaleError(AssertionError):
    """A recorded tool or argument no longer exists. The message names the tool."""


class CassetteDigestWarning(UserWarning):
    """A digest differs from the recording. Replay still runs."""


class CassetteRequest(BaseModel):
    """What the harness sent on one call. Informational; replay does not match on it."""

    model_config = ConfigDict(extra="forbid")

    tool_names: list[str] = Field(default_factory=list)
    message_count: int = Field(default=0, ge=0)


class CassetteCall(BaseModel):
    """One ``stream()`` call: its request summary and the events the model produced."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    request: CassetteRequest = Field(default_factory=CassetteRequest)
    events: list[ScriptEvent]


class CassetteConstraint(BaseModel):
    """One typed constraint from the classifier. ``source_entry_id`` is a ledger id, not replay input."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    pattern: str
    verbatim: str = ""


class CassetteClassifierRow(BaseModel):
    """One classification, in the order the ledger stored human messages."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    relates_to: str | None = None
    constraints: list[CassetteConstraint] = Field(default_factory=list)
    done_when: list[str] = Field(default_factory=list)


class CassetteJudgeCompletion(BaseModel):
    """One judge reply, in the order verdicts were committed.

    The transcript stores ``judge_tokens`` as one total. That total is
    ``output_tokens``; ``input_tokens`` stays 0 because the split was not recorded.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["valid", "malformed", "timeout"] = "valid"
    body: str = ""
    hang_s: float = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Cassette(BaseModel):
    """One recorded trajectory.

    ``calls`` are the agent's replies. ``classifier`` and ``judge`` are the
    drift checker's decisions from that same run. They are not model replies,
    so replay feeds them to the scripted classifier and the scripted judge.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["trajectory"]
    recorded_at: str = ""
    harness_commit: str = ""
    provider: str = ""
    model: str = ""
    tool_schema_digest: str | None = None
    prompt_digest: str | None = None
    source_transcript: str | None = None
    scenario: str = ""
    calls: list[CassetteCall] = Field(min_length=1)
    tool_results: dict[str, str] = Field(default_factory=dict)
    classifier: list[CassetteClassifierRow] = Field(default_factory=list)
    judge: list[CassetteJudgeCompletion] = Field(default_factory=list)


def load_cassette(path: Path) -> Cassette:
    """Read and validate one cassette file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cassette = Cassette.model_validate(raw)
    for position, call in enumerate(cassette.calls):
        if call.index != position:
            raise ValueError(f"{path}: call at position {position} has index {call.index}")
    return cassette


def dump_cassette(cassette: Cassette, path: Path) -> None:
    """Write ``cassette`` as stable, reviewable JSON (two-space indent, trailing newline)."""
    body = cassette.model_dump(mode="json", exclude_none=True)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def recording_requested() -> bool:
    """True when ``--record`` or ``MONKEYBOT_RECORD_CASSETTES=1`` asked for a re-record."""
    return os.environ.get(RECORD_ENV, "").strip().lower() in _TRUTHY


def tool_schema_digest(tools: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 of the sorted tool names and their input schemas."""
    ordered = sorted(
        ((str(tool.get("name") or ""), tool.get("input_schema")) for tool in tools),
        key=lambda pair: pair[0],
    )
    return _sha256(ordered)


def prompt_digest(system_text: str) -> str:
    """SHA-256 of the stable system prefix of ``system_text``."""
    return hashlib.sha256(stable_prompt_prefix(system_text).encode("utf-8")).hexdigest()


def stable_prompt_prefix(system_text: str) -> str:
    """The harness block before ``### Runtime paths``.

    Per-run text (workspace path, memory, MCP, skills, current date) comes later,
    so two runs of the same harness hash the same. A prompt with no harness block
    falls back to everything before ``## Current date``.
    """
    start = system_text.find(_PREFIX_START)
    if start >= 0:
        end = system_text.find(_PREFIX_END, start)
        return system_text[start : end if end >= 0 else len(system_text)]
    return system_text.split(_DATE_MARK, 1)[0]


def stale_tool(call: CassetteCall, tools: Sequence[Any]) -> str | None:
    """Name of the first tool ``call`` uses that ``tools`` no longer supports, or ``None``.

    ``tools`` are the current ``ToolDef`` objects (``name`` and ``input_schema``).
    A tool is stale when it is missing, or when an argument key is not one of its
    schema ``properties``. A schema with no ``properties`` key is not checked.
    """
    by_name = {str(tool.name): tool for tool in tools}
    for event in call.events:
        if not isinstance(event, ToolCallEvent):
            continue
        tool = by_name.get(event.name)
        if tool is None:
            return event.name
        schema = getattr(tool, "input_schema", None)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            continue
        if any(key not in properties for key in event.args):
            return event.name
    return None


def stale_message(name: str) -> str:
    return f"cassette_stale: tool {name} changed"


def digest_warnings(
    cassette: Cassette,
    *,
    tools: Sequence[Mapping[str, Any]],
    system_text: str,
) -> list[str]:
    """Differences between the recording and the first live request. Warnings, not failures."""
    found: list[str] = []
    if cassette.tool_schema_digest and cassette.tool_schema_digest != tool_schema_digest(tools):
        found.append("tool_schema_digest differs from the recording")
    if cassette.prompt_digest and cassette.prompt_digest != prompt_digest(system_text):
        found.append("prompt_digest differs from the recording")
    current = {str(tool.get("name") or "") for tool in tools}
    first = cassette.calls[0].request.tool_names
    missing = sorted(name for name in first if name not in current)
    if missing:
        found.append("advertised tools no longer present: " + ", ".join(missing))
    return found


def _sha256(value: object) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "RECORD_ENV",
    "SCHEMA_VERSION",
    "Cassette",
    "CassetteCall",
    "CassetteDigestWarning",
    "CassetteRequest",
    "CassetteStaleError",
    "digest_warnings",
    "dump_cassette",
    "load_cassette",
    "prompt_digest",
    "recording_requested",
    "stable_prompt_prefix",
    "stale_message",
    "stale_tool",
    "tool_schema_digest",
]
