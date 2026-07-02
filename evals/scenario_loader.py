"""YAML scenario parsing + disk discovery, shared by the FastAPI service and the CLI report."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

import yaml
from models import Scenario

EVAL_ROOT = Path(__file__).resolve().parent


def parse_scenario_yaml(
    text: str, *, sid: str | None, source: Literal["builtin", "operator"]
) -> Scenario:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML must be a mapping at the top level")
    stem = (sid or str(data.get("id") or "").strip() or uuid.uuid4().hex[:12]).strip()
    messages = data.get("messages") or []
    sessions_raw = data.get("sessions")
    sessions: list[list[str]] | None = None
    if sessions_raw is not None:
        if not isinstance(sessions_raw, list) or not sessions_raw:
            raise ValueError("sessions must be a non-empty list of message lists")
        sessions = []
        for group in sessions_raw:
            if not isinstance(group, list) or not group or not all(isinstance(m, str) for m in group):
                raise ValueError("each sessions group must be a non-empty list of strings")
            sessions.append(list(group))
    elif not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list of strings (or set sessions)")
    if not all(isinstance(m, str) for m in messages):
        raise ValueError("each message must be a string")
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        raise ValueError("tags must be a list of strings")
    assertions = data.get("assertions") or {}
    if assertions is not None and not isinstance(assertions, dict):
        raise ValueError("assertions must be a mapping")
    return Scenario(
        id=stem,
        description=str(data.get("description") or ""),
        tags=[str(t) for t in tags],
        messages=list(messages),
        sessions=sessions,
        session_pause_sec=float(data.get("session_pause_sec", 5.0)),
        assertions=dict(assertions),
        source=source,
    )


def load_disk_scenarios(root: Path = EVAL_ROOT / "scenarios") -> list[Scenario]:
    """Load built-in scenarios from subsystem folders (``tools/``, ``memory/``, ...) plus operator/.

    A built-in scenario id is its path relative to ``scenarios/`` without the ``.yaml``
    extension (e.g. ``tools/core_read``), matching how ``evals/suites/*.yaml`` reference them.
    """
    scenarios: list[Scenario] = []
    if not root.is_dir():
        return scenarios
    op = root / "operator"
    for path in sorted(root.rglob("*.yaml")):
        if op in path.parents:
            continue
        raw = path.read_text(encoding="utf-8")
        rel_id = path.relative_to(root).with_suffix("").as_posix()
        scenarios.append(parse_scenario_yaml(raw, sid=rel_id, source="builtin"))
    if op.is_dir():
        for path in sorted(op.glob("*.yaml")):
            raw = path.read_text(encoding="utf-8")
            scenarios.append(parse_scenario_yaml(raw, sid=path.stem, source="operator"))
    return scenarios


def load_scenarios_by_id(scenario_ids: list[str], root: Path = EVAL_ROOT / "scenarios") -> list[Scenario]:
    """Load exactly the scenarios named in a suite manifest, in that order."""
    by_id = {s.id: s for s in load_disk_scenarios(root)}
    missing = [sid for sid in scenario_ids if sid not in by_id]
    if missing:
        raise KeyError(f"Unknown scenario id(s) in suite manifest: {missing}")
    return [by_id[sid] for sid in scenario_ids]


def load_suite(path: Path) -> tuple[str, list[str]]:
    """Parse a suite manifest YAML; returns ``(suite_id, scenario_ids)``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Suite manifest must be a mapping: {path}")
    suite_id = str(data.get("id") or path.stem)
    scenarios = data.get("scenarios") or []
    if not isinstance(scenarios, list) or not all(isinstance(s, str) for s in scenarios):
        raise ValueError(f"Suite manifest 'scenarios' must be a list of strings: {path}")
    return suite_id, list(scenarios)
