"""Regression guard: subagents must never get ``computer_*`` tools.

``subagent_worker.py`` builds its own ``extra_tools`` list independently of the
gateway's ``_deps.computer_tools`` (see ``core/subagents/subagent_worker.py``,
which only ever appends the web-search tool) and loads permissions with
``allow_ask=False`` (``ask`` -> ``deny``), so even if a computer tool were
somehow advertised, a "confirm" decision could never be reached in a subagent
(no session to prompt). This test pins the first, cheaper invariant: nothing
in the subagent worker module imports the computer package at all — a
`monkeybot.computer` import appearing there would be a signal that tools_list
construction changed shape and needs re-review against this scope decision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import monkeybot.core.subagents.subagent_worker as subagent_worker


def test_subagent_worker_does_not_import_computer_package() -> None:
    source_path = Path(subagent_worker.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    computer_imports = {m for m in imported_modules if m.startswith("monkeybot.computer")}
    assert not computer_imports, (
        f"subagent_worker.py must not import computer package: {computer_imports}"
    )
