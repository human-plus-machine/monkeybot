"""In-memory stores for runs and scenarios (single-process eval service)."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

from models import EvalRun, Scenario


class EvalStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, EvalRun] = {}
        self._scenarios: dict[str, Scenario] = {}

    def put_run(self, run: EvalRun) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def get_run(self, run_id: str) -> EvalRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(self) -> list[EvalRun]:
        with self._lock:
            return list(self._runs.values())

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            if run_id not in self._runs:
                return False
            del self._runs[run_id]
            return True

    def replace_scenarios(self, scenarios: Iterable[Scenario]) -> None:
        with self._lock:
            self._scenarios = {s.id: s for s in scenarios}

    def get_scenario(self, sid: str) -> Scenario | None:
        with self._lock:
            return self._scenarios.get(sid)

    def list_scenarios(self) -> list[Scenario]:
        with self._lock:
            return sorted(self._scenarios.values(), key=lambda s: (s.source, s.id))

    def upsert_scenario(self, scenario: Scenario) -> None:
        with self._lock:
            self._scenarios[scenario.id] = scenario

    def delete_scenario(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._scenarios:
                return False
            if self._scenarios[sid].source == "builtin":
                return False
            del self._scenarios[sid]
            return True
