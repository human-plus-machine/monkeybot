"""Mark everything collected under tests/unit as tier 1."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    unit = Path(config.rootpath) / "tests" / "unit"
    for item in items:
        path = Path(str(item.path))
        if path == unit or unit in path.parents:
            item.add_marker(pytest.mark.t1)
