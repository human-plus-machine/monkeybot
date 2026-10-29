"""Live scenario loading for the T4 runner: a thin wrapper over ``regression/schema``.

Parsing and validation belong to the unified schema (§5.2): ``case`` is required, unknown keys
and assertion keys outside ``mode: live`` are errors, and ``verifier_on`` / ``verifier_off``
are a load error while ``EVAL_VERIFIER_MODE`` is unset. This module turns a validated scenario
into the runner's :class:`regression.t4.models.Scenario` and copies workspace fixtures.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from regression.fixtures.build import refresh_seeded_attachment_clocks
from regression.schema.scenario import Scenario as SchemaScenario
from regression.schema.scenario import load_scenario
from regression.t4.models import Scenario

EVAL_ROOT = Path(__file__).resolve().parent
SCENARIOS_ROOT = EVAL_ROOT / "scenarios"
# Fixture repos and overlays. ``workspace_fixture`` and ``workspace_overlays`` are
# relative to this directory (``calc_repo``, ``overlays/bug_discount``).
FIXTURES_ROOT = EVAL_ROOT.parent / "fixtures" / "workspaces"
# The running agent's default workspace (the agent project under ``regression/t4/agent``).
_AGENT_WORKSPACE = EVAL_ROOT / "agent" / "workspace"
_DEFAULT_SESSION_PAUSE_SEC = 5.0


def parse_scenario_yaml(text: str, *, sid: str | None) -> Scenario:
    """Validate one live scenario's YAML text; ``sid`` is its id (the path under ``scenarios/``)."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML must be a mapping at the top level")
    fields: dict[str, Any] = {str(key): value for key, value in data.items()}
    declared = str(fields.get("id") or "").strip()
    if sid and declared and declared != sid:
        raise ValueError(f"id {declared!r} does not match path id {sid!r}")
    fields["id"] = (sid or declared or uuid.uuid4().hex[:12]).strip()
    return _live(SchemaScenario.model_validate(fields))


def load_scenario_file(path: Path, root: Path = SCENARIOS_ROOT) -> Scenario:
    """Load one scenario file; its id is the path under ``root`` without ``.yaml``."""
    return _live(load_scenario(path, root=root))


def _live(scenario: SchemaScenario) -> Scenario:
    if scenario.mode != "live":
        raise ValueError(f"{scenario.id}: T4 scenarios are mode live, not {scenario.mode!r}")
    ticks = any(step.action == "invoke_tick" for step in scenario.steps)
    if scenario.sessions is not None:
        if not scenario.sessions or not all(scenario.sessions):
            raise ValueError("sessions must be a non-empty list of non-empty message lists")
    elif not scenario.messages and not ticks:
        raise ValueError(
            "messages must be a non-empty list of strings (or set sessions, "
            "or drive the turns with an invoke_tick step)"
        )
    if ticks and not any(step.action == "create_loop" for step in scenario.steps):
        raise ValueError("invoke_tick needs a create_loop step before it")
    if scenario.assertions is not None and not isinstance(scenario.assertions, dict):
        raise ValueError("assertions must be a mapping")
    pause = scenario.session_pause_sec
    return Scenario(
        id=scenario.id,
        case=scenario.case,
        description=scenario.description,
        tags=list(scenario.tags),
        messages=list(scenario.messages),
        sessions=[list(group) for group in scenario.sessions] if scenario.sessions else None,
        session_pause_sec=_DEFAULT_SESSION_PAUSE_SEC if pause is None else pause,
        assertions=dict(scenario.assertions or {}),
        workspace_fixture=(
            _relative_under_fixtures(scenario.workspace_fixture, "workspace_fixture")
            if scenario.workspace_fixture is not None
            else None
        ),
        workspace_overlays=[
            _relative_under_fixtures(item, "workspace_overlays")
            for item in scenario.workspace_overlays
        ],
        config=scenario.config.model_dump() if scenario.config is not None else {},
        steps=list(scenario.steps),
    )


def _relative_under_fixtures(raw: str, key: str) -> str:
    rel = Path(raw.strip())
    if not raw.strip() or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"{key} must be a relative path under regression/fixtures/workspaces/")
    return rel.as_posix()


def load_disk_scenarios(root: Path = SCENARIOS_ROOT) -> list[Scenario]:
    """Load every scenario under ``root`` (``file/``, ``memory/``, ...).

    A scenario id is its path relative to ``scenarios/`` without the ``.yaml``
    extension (e.g. ``file/core_read``), matching how ``regression/t4/suites/*.yaml`` reference them.
    """
    if not root.is_dir():
        return []
    return [load_scenario_file(path, root) for path in sorted(root.rglob("*.yaml"))]


def load_scenarios_by_id(scenario_ids: list[str], root: Path = SCENARIOS_ROOT) -> list[Scenario]:
    """Load exactly the scenarios named in a suite manifest, in that order."""
    missing = [sid for sid in scenario_ids if not (root / f"{sid}.yaml").is_file()]
    if missing:
        raise KeyError(f"Unknown scenario id(s) in suite manifest: {missing}")
    return [load_scenario_file(root / f"{sid}.yaml", root) for sid in scenario_ids]


PYTHON_TESTS_ROOT = EVAL_ROOT / "python"
_SUITE_KEYS = frozenset({"id", "scenarios", "python", "verifier_mode"})


class SuiteManifest(BaseModel):
    """One ``regression/t4/suites/<id>.yaml``.

    ``python`` names test files under ``regression/t4/python`` that ``pytest --suite`` also
    runs; the ``python -m regression.t4`` CLI runs scenarios only. ``verifier_mode`` is the
    ``EVAL_VERIFIER_MODE`` the suite runs with when the environment does not set one.
    """

    id: str
    scenarios: list[str]
    python: list[str] = Field(default_factory=list)
    verifier_mode: Literal["on", "off"] | None = None


def load_suite_manifest(path: Path) -> SuiteManifest:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Suite manifest must be a mapping: {path}")
    unknown = sorted(set(data) - _SUITE_KEYS)
    if unknown:
        raise ValueError(f"Suite manifest has unknown keys {unknown}: {path}")
    scenarios = data.get("scenarios") or []
    if not isinstance(scenarios, list) or not all(isinstance(s, str) for s in scenarios):
        raise ValueError(f"Suite manifest 'scenarios' must be a list of strings: {path}")
    python = data.get("python") or []
    if not isinstance(python, list) or not all(isinstance(p, str) for p in python):
        raise ValueError(f"Suite manifest 'python' must be a list of file names: {path}")
    missing = [name for name in python if not (PYTHON_TESTS_ROOT / name).is_file()]
    if missing:
        raise ValueError(f"Suite manifest 'python' names missing files {missing}: {path}")
    mode = data.get("verifier_mode")
    if mode is not None and mode not in ("on", "off"):
        raise ValueError(f"Suite manifest 'verifier_mode' must be on or off: {path}")
    return SuiteManifest(
        id=str(data.get("id") or path.stem),
        scenarios=list(scenarios),
        python=list(python),
        verifier_mode=mode,
    )


def load_suite(path: Path) -> tuple[str, list[str]]:
    """Parse a suite manifest YAML; returns ``(suite_id, scenario_ids)``."""
    manifest = load_suite_manifest(path)
    return manifest.id, list(manifest.scenarios)


def agent_workspace_root() -> Path:
    """Workspace root of the running eval agent (``MONKEYBOT_AGENT_WORKSPACE`` or the agent project's)."""
    raw = os.environ.get("MONKEYBOT_AGENT_WORKSPACE", "").strip()
    return Path(raw).expanduser() if raw else _AGENT_WORKSPACE


def fixture_source(scenario: Scenario) -> Path | None:
    return FIXTURES_ROOT / scenario.workspace_fixture if scenario.workspace_fixture else None


def fixture_file(scenario: Scenario, rel: str) -> Path | None:
    """Pristine copy of ``rel`` as the agent first saw it: the last overlay that has it, else the fixture."""
    for overlay in reversed(scenario.workspace_overlays):
        candidate = FIXTURES_ROOT / overlay / rel
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
        overlay_dir = FIXTURES_ROOT / overlay
        if not overlay_dir.is_dir():
            raise FileNotFoundError(f"workspace_overlay not found: {overlay_dir}")
        shutil.copytree(overlay_dir, dest, dirs_exist_ok=True)
    refresh_seeded_attachment_clocks(dest)

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
