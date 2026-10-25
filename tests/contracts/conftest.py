"""Mark contract tests as tier 1 and print catalog coverage after the pairing run."""

from __future__ import annotations

from pathlib import Path

import pytest

UNCOVERED_LINES: list[str] = []


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).resolve().parent
    for item in items:
        path = Path(str(item.path))
        if path == here or here in path.parents:
            item.add_marker(pytest.mark.t1)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    del exitstatus, config
    if not UNCOVERED_LINES:
        return
    terminalreporter.write_sep("-", "uncovered catalog rows")
    for line in UNCOVERED_LINES:
        terminalreporter.write_line(line)
