"""Keyed T5 baseline lookup.

The key is ``<suite>[@<config>]__<provider>__<model>.json`` under
``regression/t5/baselines/``. A missing file is no baseline. A file for a
different suite, config, provider, or model is never read. Only
``write_baseline`` creates or replaces a file, and only ``--update-baseline``
calls it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
SCHEMA_VERSION = 1


def baseline_filename(
    suite: str,
    provider: str,
    model: str,
    config: str | None = None,
) -> str:
    """``coding_hard__ollama-cloud__glm-5.3-flash.json``, or ``drift@verifier-judge__…``."""
    head = _segment(suite, "suite")
    if config:
        head = f"{head}@{_segment(config, 'config')}"
    return f"{head}__{_segment(provider, 'provider')}__{_segment(model, 'model')}.json"


def baseline_path(
    suite: str,
    provider: str,
    model: str,
    config: str | None = None,
    *,
    directory: Path | None = None,
) -> Path:
    return (directory or BASELINES_DIR) / baseline_filename(suite, provider, model, config)


def lookup(
    suite: str,
    provider: str,
    model: str,
    config: str | None = None,
    *,
    directory: Path | None = None,
) -> dict[str, Any] | None:
    """The baseline for this exact key, or ``None`` when the file is absent or mislabeled."""
    path = baseline_path(suite, provider, model, config, directory=directory)
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return None
    if loaded.get("suite") != suite or loaded.get("provider") != provider:
        return None
    if loaded.get("model") != model:
        return None
    stored = loaded.get("config") or None
    if stored != (config or None):
        return None
    return loaded


def scenario_counts(document: dict[str, Any] | None, scenario_id: str) -> tuple[int, int] | None:
    """``(passed, runs)`` for one scenario, or ``None`` when this baseline does not have it."""
    if document is None:
        return None
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, dict):
        return None
    row = scenarios.get(scenario_id)
    if not isinstance(row, dict) or "passed" not in row or "runs" not in row:
        return None
    passed = int(row["passed"])
    runs = int(row["runs"])
    if runs < 1 or not 0 <= passed <= runs:
        return None
    return passed, runs


def baseline_document(
    *,
    suite: str,
    provider: str,
    model: str,
    config: str | None,
    trials: int,
    scenarios: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": suite,
        "config": config,
        "provider": provider,
        "model": model,
        "trials": trials,
        "scenarios": {
            scenario_id: {"passed": passed, "runs": runs}
            for scenario_id, (passed, runs) in scenarios.items()
        },
    }


def write_baseline(path: Path, document: dict[str, Any]) -> None:
    """Write one baseline file. Callers other than ``--update-baseline`` must not call this."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _segment(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"baseline {label} must be a single path segment, got {value!r}")
    return value
