"""Pair docs/testing.md §8 with case IDs cited by tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from testkit.failures.catalog import (
    Catalog,
    CatalogRow,
    catalog_expect,
    folder_tier,
    load_catalog,
    parse_case_id,
)
from testkit.failures.store import repo_root
from tests.contracts import conftest as contracts

_CASE_LINE = re.compile(r"^case:\s+['\"]?([^'\"#\s]+)['\"]?\s*(?:#.*)?$")
_ROOTS = ("regression", "tests")


def test_catalog_pairing() -> None:
    catalog = load_catalog()
    _assert_seed_shape(catalog)
    violations = _pairing_violations(catalog)
    lines = list(catalog.uncovered_lines())
    contracts.UNCOVERED_LINES[:] = lines
    for line in lines:
        print(line)
    assert not violations, "\n".join(violations)


def _assert_seed_shape(catalog: Catalog) -> None:
    assert len(catalog.rows) == 419
    filled = [row for row in catalog.rows if row.tests]
    assert len(filled) == 18
    assert sum(len(row.tests) for row in filled) == 19
    ranged = catalog.covering("T4-CODE-05")
    assert ranged is not None and ranged.raw_id == "T4-CODE-04..10"
    earlier = catalog.covering("T4-CODE-03")
    assert earlier is not None and earlier.raw_id == "T4-CODE-01..03"
    gateway = catalog.covering("T2-ATT-G1")
    assert gateway is not None and gateway.raw_id == "T2-ATT-G1"
    assert catalog.covering("T4-CODE-11") is None or catalog.covering("T4-CODE-11").raw_id != (
        "T4-CODE-04..10"
    )
    shell = catalog.covering("T2-SHELL-15")
    assert shell is not None and "edit|update|forget|repair" in shell.expect
    masked = catalog.covering("T2-CODE-05")
    assert masked is not None and "pytest | tee x" in masked.expect
    assert catalog_expect("T2-LOOP-01") == "Text-only turn completes, one provider call."
    assert catalog_expect("T4-CODE-05") == ranged.expect


def _pairing_violations(catalog: Catalog) -> list[str]:
    root = repo_root()
    violations: list[str] = []
    seen: dict[tuple[str, str, str], str] = {}
    for row in catalog.rows:
        for key in row.identity_keys():
            previous = seen.get(key)
            if previous is not None:
                violations.append(f"{row.raw_id} repeats {previous}")
            else:
                seen[key] = row.raw_id
        for listed in row.tests:
            violations.extend(_path_violations(root, row, listed, cited=False))
    for path, case_id in _citations(root):
        parsed = parse_case_id(case_id)
        row = catalog.covering(case_id) if parsed is not None else None
        if row is None:
            violations.append(f"{path} cites {case_id}, which is not in the catalog")
            continue
        if path not in row.tests:
            violations.append(
                f"{path} cites {row.raw_id} but is not listed in that row's Test cell"
            )
        violations.extend(_path_violations(root, row, path, cited=True))
    return list(dict.fromkeys(violations))


def _path_violations(root: Path, row: CatalogRow, listed: str, *, cited: bool) -> list[str]:
    violations: list[str] = []
    full = root / listed
    if not cited and not full.is_file():
        violations.append(f"{row.raw_id} Test cell names {listed}, which does not exist")
    home = folder_tier(listed)
    if home != row.tier:
        violations.append(
            f"{row.raw_id} lives in {listed}, which is {home or 'outside a tier folder'}, "
            f"not {row.tier}"
        )
    return violations


def _citations(root: Path) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for name in _ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix in {".yaml", ".yml"}:
                for case_id in _yaml_cases(path):
                    found.add((relative, case_id))
            elif path.suffix == ".py":
                for case_id in _python_cases(path):
                    found.add((relative, case_id))
    return sorted(found)


def _yaml_cases(path: Path) -> list[str]:
    cases: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _CASE_LINE.match(line.strip())
        if match is not None:
            cases.append(match.group(1))
    return cases


def _python_cases(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    cases: list[str] = []
    for node in ast.walk(tree):
        decorators = getattr(node, "decorator_list", None)
        if not decorators:
            continue
        for decorator in decorators:
            if not isinstance(decorator, ast.Call) or not _is_case_mark(decorator.func):
                continue
            if not decorator.args:
                continue
            arg = decorator.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                cases.append(arg.value)
    return cases


def _is_case_mark(node: ast.expr) -> bool:
    if not isinstance(node, ast.Attribute) or node.attr != "case":
        return False
    mark = node.value
    if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
        return False
    return isinstance(mark.value, ast.Name) and mark.value.id == "pytest"
