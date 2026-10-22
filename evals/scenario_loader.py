"""YAML scenario parsing + disk discovery for the eval report CLI."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import yaml
from models import Scenario

EVAL_ROOT = Path(__file__).resolve().parent


def parse_scenario_yaml(text: str, *, sid: str | None) -> Scenario:
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
            if (
                not isinstance(group, list)
                or not group
                or not all(isinstance(m, str) for m in group)
            ):
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
        workspace_fixture=_opt_fixture(data),
        workspace_overlays=_opt_overlays(data),
    )


def _relative_under_evals(raw: object, key: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{key} entries must be non-empty strings")
    rel = Path(raw.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"{key} must be a relative path under evals/")
    return rel.as_posix()


def _opt_overlays(data: dict) -> list[str]:
    raw = data.get("workspace_overlays")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("workspace_overlays must be a list")
    return [_relative_under_evals(item, "workspace_overlays") for item in raw]


def _opt_fixture(data: dict) -> str | None:
    raw = data.get("workspace_fixture")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace_fixture must be a non-empty string")
    rel = Path(raw.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("workspace_fixture must be a relative path under evals/")
    return rel.as_posix()


def load_disk_scenarios(root: Path = EVAL_ROOT / "scenarios") -> list[Scenario]:
    """Load scenarios from subsystem folders (``tools/``, ``memory/``, ...).

    A scenario id is its path relative to ``scenarios/`` without the ``.yaml``
    extension (e.g. ``tools/core_read``), matching how ``evals/suites/*.yaml`` reference them.
    """
    scenarios: list[Scenario] = []
    if not root.is_dir():
        return scenarios
    for path in sorted(root.rglob("*.yaml")):
        raw = path.read_text(encoding="utf-8")
        rel_id = path.relative_to(root).with_suffix("").as_posix()
        scenarios.append(parse_scenario_yaml(raw, sid=rel_id))
    return scenarios


def load_scenarios_by_id(
    scenario_ids: list[str], root: Path = EVAL_ROOT / "scenarios"
) -> list[Scenario]:
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


def agent_workspace_root() -> Path:
    """Workspace root of the running eval agent (``MONKEYBOT_AGENT_WORKSPACE`` or the smoke agent's)."""
    raw = os.environ.get("MONKEYBOT_AGENT_WORKSPACE", "").strip()
    return Path(raw).expanduser() if raw else EVAL_ROOT / "smoke_agent" / "workspace"


def fixture_source(scenario: Scenario) -> Path | None:
    return EVAL_ROOT / scenario.workspace_fixture if scenario.workspace_fixture else None


def fixture_file(scenario: Scenario, rel: str) -> Path | None:
    """Pristine copy of ``rel`` as the agent first saw it: the last overlay that has it, else the fixture."""
    for overlay in reversed(scenario.workspace_overlays):
        candidate = EVAL_ROOT / overlay / rel
        if candidate.is_file():
            return candidate
    source = fixture_source(scenario)
    if source is not None and (source / rel).is_file():
        return source / rel
    return None


def workspace_subdir_name(scenario: Scenario) -> str:
    """Per-scenario directory name inside the agent workspace (``eval-<id>``, slashes flattened)."""
    return "eval-" + scenario.id.replace("/", "-")


def materialize_workspace(
    scenario: Scenario, workspace_root: Path | None = None
) -> tuple[Scenario, Path | None]:
    """Copy the scenario's fixture into the agent workspace; return the scenario with
    ``{workspace_subdir}`` substituted into its messages, plus the copied directory.

    Scenarios without a ``workspace_fixture`` are returned unchanged with ``None``.
    The copy is recreated on every run so a previous run's edits never leak in.
    """
    source = fixture_source(scenario)
    if source is None:
        return scenario, None
    if not source.is_dir():
        raise FileNotFoundError(f"workspace_fixture not found: {source}")
    subdir = workspace_subdir_name(scenario)
    dest = (workspace_root or agent_workspace_root()) / subdir
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    for overlay in scenario.workspace_overlays:
        overlay_dir = EVAL_ROOT / overlay
        if not overlay_dir.is_dir():
            raise FileNotFoundError(f"workspace_overlay not found: {overlay_dir}")
        shutil.copytree(overlay_dir, dest, dirs_exist_ok=True)

    def sub(text: str) -> str:
        return text.replace("{workspace_subdir}", subdir)

    resolved = scenario.model_copy(
        update={
            "messages": [sub(m) for m in scenario.messages],
            "sessions": (
                [[sub(m) for m in group] for group in scenario.sessions]
                if scenario.sessions
                else scenario.sessions
            ),
        }
    )
    return resolved, dest
