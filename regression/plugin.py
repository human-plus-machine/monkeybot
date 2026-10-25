"""Pytest plugin for the regression tree.

Loaded once for every test root by ``-p regression.plugin`` in ``pytest.ini``.
Options live here only: a second ``addoption`` in any conftest makes pytest
fail with "option names already added".
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_LATER = "Unused until a later task."


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("regression", "Regression tiers")
    group.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Collect T4 and T5 items. Without it those items are deselected.",
    )
    group.addoption(
        "--record",
        action="store_true",
        default=False,
        help=f"Re-record T3 cassettes. {_LATER}",
    )
    group.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite goldens. Refused when CI is set. Otherwise unused.",
    )
    group.addoption(
        "--trials",
        action="store",
        default=None,
        help=f"Trial count for a T5 run. {_LATER}",
    )
    group.addoption(
        "--suite",
        action="store",
        default=None,
        help=f"Live or trial suite name. {_LATER}",
    )
    group.addoption(
        "--agent-url",
        action="store",
        default=None,
        help=f"Base URL of a live agent. {_LATER}",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("update_golden") and os.environ.get("CI", "").strip():
        raise pytest.UsageError("--update-golden is refused when CI is set")


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    """Turn each ``*.yaml`` under ``regression/t2/**/scenarios/`` into one item.

    T3 and T4 globs are later tasks. ``.yml`` is not collected.
    """
    if file_path.suffix != ".yaml":
        return None
    root = Path(str(parent.config.rootpath)).resolve()
    try:
        relative = file_path.resolve().relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 4 or parts[0] != "regression" or parts[1] != "t2":
        return None
    if "scenarios" not in parts[:-1]:
        return None
    return ScenarioFile.from_parent(parent, path=file_path)


class ScenarioFile(pytest.File):
    """One scenario YAML file. Collection sets the ledger case id."""

    def collect(self) -> object:
        from regression.schema.scenario import load_scenario

        scenario = load_scenario(self.path)
        item = ScenarioItem.from_parent(self, name=self.path.stem)
        item.user_properties.append(("case", scenario.case))
        yield item


class ScenarioItem(pytest.Item):
    """Runs one YAML scenario through the loop driver."""

    def runtest(self) -> None:
        from regression.drivers.loop import run_scenario
        from regression.schema.scenario import load_scenario

        scenario = load_scenario(self.path)
        asyncio.run(run_scenario(scenario))

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: str | None = None,
    ) -> str | pytest.TerminalRepr:
        if isinstance(excinfo.value, AssertionError):
            return str(excinfo.value)
        return excinfo.getrepr(style=style)

    def reportinfo(self) -> tuple[Path, None, str]:
        return self.path, None, self.path.name
