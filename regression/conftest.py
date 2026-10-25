"""Mark regression items by the tier directory they live in."""

from __future__ import annotations

from pathlib import Path

import pytest

_TIERS = ("t1", "t2", "t3", "t4", "t5")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    base = Path(config.rootpath) / "regression"
    for item in items:
        path = Path(str(item.path))
        if path != base and base not in path.parents:
            continue
        tier = path.relative_to(base).parts[0]
        if tier == "selftest":
            item.add_marker(pytest.mark.t1)
        elif tier in _TIERS:
            item.add_marker(getattr(pytest.mark, tier))
