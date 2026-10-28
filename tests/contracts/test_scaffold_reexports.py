"""T1-OPS-01. monkeybot.scaffold re-exports names that exist on monkeybot_cli.scaffold."""

from __future__ import annotations

import ast
from pathlib import Path

import monkeybot_cli.scaffold as cli
import pytest

import monkeybot


def _shim_exports() -> list[str]:
    """Names ``monkeybot.scaffold`` re-exports, read without executing the import.

    Importing the shim raises when a listed name is missing, which would hide the
    missing name behind a "CLI not installed" error. The contract is the name list.
    """
    path = Path(monkeybot.__file__).resolve().parent / "scaffold" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            continue
        elts = node.value.elts  # type: ignore[attr-defined]
        return [elt.value for elt in elts if isinstance(elt, ast.Constant)]
    raise AssertionError("monkeybot.scaffold has no __all__")


@pytest.mark.case("T1-OPS-01")
def test_scaffold_reexports_exist_on_cli() -> None:
    """Every name monkeybot.scaffold re-exports exists on monkeybot_cli.scaffold."""
    names = _shim_exports()
    assert names
    missing = [name for name in names if not hasattr(cli, name)]
    assert not missing, missing
    import monkeybot.scaffold as shim

    for name in names:
        assert getattr(shim, name) is getattr(cli, name)
