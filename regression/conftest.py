"""Mark regression items by the tier directory they live in.

T4 and T5 stay out of a default run. ``--live`` (registered in
``regression.plugin``) collects them. The other regression options are
registered there and do nothing in this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TIERS = ("t1", "t2", "t3", "t4", "t5")
_LIVE_TIERS = ("t4", "t5")


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
    if sys.platform != "darwin":
        for item in items:
            if item.get_closest_marker("darwin_only") is not None:
                item.add_marker(pytest.mark.skip(reason="darwin-only scenario"))
    if config.getoption("live"):
        return
    dropped = [item for item in items if any(item.get_closest_marker(name) for name in _LIVE_TIERS)]
    if not dropped:
        return
    config.hook.pytest_deselected(items=dropped)
    dropped_ids = {id(item) for item in dropped}
    items[:] = [item for item in items if id(item) not in dropped_ids]
