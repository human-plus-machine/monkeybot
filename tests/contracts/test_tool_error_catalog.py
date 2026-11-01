"""T1-FILE-01 and T1-FILE-02. Appendix A codes and the built-in error envelope."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_APPENDIX = _REPO / "regression" / "plan" / "reference" / "appendices.md"
_GOLDEN = Path(__file__).resolve().parent / "goldens" / "tool_error_codes.txt"
_CODE = re.compile(r"`([a-z][a-z0-9_]*)`")
_ERROR_KINDS = frozenset(
    {
        "validation",
        "policy",
        "runtime",
        "incomplete_scan",
        "credential_egress_blocked",
        "pending",
        "background_unavailable",
    }
)
_ENVELOPE_HELPERS = frozenset(
    {
        "_built_in_tool_error",
        "_workspace_error_envelope",
        "_incomplete_scan_envelope",
        "_read_ledger_error",
        "_run_command_parse_envelope",
        "_run_command_security_envelope",
    }
)
# Known envelope exceptions, named so a new one fails the scan.
_ENABLE_MCP = frozenset({"_tool_enable_mcp"})
_SOME_LOAD_FILE = frozenset(
    {
        "_tool_load_file",
        "_load_file_from_attachment",
        "_load_file_from_path",
        "_media_result",
    }
)
_COMPUTER_TOOLS = "computer_*"


def _appendix_codes() -> list[str]:
    text = _APPENDIX.read_text(encoding="utf-8")
    section = text.split("## Appendix B", 1)[0]
    found: set[str] = set()
    for paragraph in re.split(r"\n\s*\n", section):
        if "No `details.code`" in paragraph:
            continue
        found.update(_CODE.findall(paragraph))
    return sorted(found)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _literal_code(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _constructor_codes(tree: ast.AST, class_name: str) -> set[str]:
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != class_name:
            continue
        if len(node.args) >= 2:
            positional = _literal_code(node.args[1])
            if positional is not None:
                codes.add(positional)
        for keyword in node.keywords:
            if keyword.arg == "code":
                explicit = _literal_code(keyword.value)
                if explicit is not None:
                    codes.add(explicit)
        if not any(keyword.arg == "code" for keyword in node.keywords) and len(node.args) < 2:
            codes.add(_default_code(tree, class_name))
    default = _default_code(tree, class_name)
    if default:
        codes.add(default)
    return {code for code in codes if code}


def _default_code(tree: ast.AST, class_name: str) -> str:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            args = item.args.args
            defaults = item.args.defaults
            offset = len(args) - len(defaults)
            for index, arg in enumerate(args):
                if arg.arg != "code":
                    continue
                default_index = index - offset
                if default_index < 0 or default_index >= len(defaults):
                    return ""
                literal = _literal_code(defaults[default_index])
                return literal or ""
    return ""


def _write_problem_literals(tree: ast.AST) -> set[str]:
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "WriteProblem":
            continue
        annotation = node.annotation
        if not isinstance(annotation, ast.Subscript):
            continue
        slice_node = annotation.slice
        elts = slice_node.elts if isinstance(slice_node, ast.Tuple) else [slice_node]
        for elt in elts:
            literal = _literal_code(elt)
            if literal is not None:
                codes.add(literal)
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            literal = _literal_code(node.value) if node.value is not None else None
            if literal is not None:
                codes.add(literal)
    return codes


def _raised_codes() -> set[str]:
    codes: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        codes.update(_constructor_codes(tree, "WorkspaceError"))
        codes.update(_constructor_codes(tree, "PatchError"))
        if path.name == "read_ledger.py":
            codes.update(_write_problem_literals(tree))
    return codes


def _plain_error(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.Call) and _call_name(node) == "str"


def _error_expr(node: ast.Return) -> ast.expr | None:
    value = node.value
    if isinstance(value, ast.Tuple) and len(value.elts) == 2:
        err = value.elts[1]
        if isinstance(err, ast.Constant) and err.value is None:
            return None
        return err
    if isinstance(value, ast.Call) and _call_name(value) == "err":
        return value.args[0] if value.args else None
    return None


def _kind_violations(path: Path, tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "_built_in_tool_error":
            continue
        if not node.args:
            continue
        kind = _literal_code(node.args[0])
        if kind is not None and kind not in _ERROR_KINDS:
            found.append(f"{path}:{node.lineno} error_kind {kind!r}")
    return found


def _shape_violations(path: Path, tree: ast.AST) -> list[str]:
    if path.name == "tools.py" and "computer" in path.parts:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _ENABLE_MCP or node.name in _SOME_LOAD_FILE:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or child in _nested_returns(node):
                continue
            err = _error_expr(child)
            if err is None or not _plain_error(err):
                continue
            if isinstance(err, ast.Call) and _call_name(err) in _ENVELOPE_HELPERS:
                continue
            found.append(f"{path.name}:{child.lineno} {node.name} plain tool error")
    return found


def _nested_returns(fn: ast.AST) -> set[ast.Return]:
    """Returns that belong to a function nested inside ``fn``."""
    nested: set[ast.Return] = set()
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested.update(node for node in ast.walk(child) if isinstance(node, ast.Return))
    return nested


def _commit(text: str, *, update: bool) -> None:
    if update:
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(text, encoding="utf-8")
        return
    assert _GOLDEN.is_file(), "missing tool_error_codes.txt; re-run with --update-golden"
    assert _GOLDEN.read_text(encoding="utf-8") == text


@pytest.mark.case("T1-FILE-01")
def test_raised_error_codes_are_in_appendix_a(request: pytest.FixtureRequest) -> None:
    """Every details.code raised in src is in Appendix A's enum."""
    appendix = _appendix_codes()
    golden = "\n".join(appendix) + "\n"
    _commit(golden, update=bool(request.config.getoption("update_golden")))
    committed = {
        line.strip() for line in _GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    assert committed == set(appendix)
    extra = sorted(_raised_codes() - committed)
    assert not extra, "undocumented details.code: " + ", ".join(extra)


@pytest.mark.case("T1-FILE-02")
def test_builtin_tool_errors_use_the_envelope() -> None:
    """Built-in tool errors use the envelope. enable_mcp, some load_file, and computer_* do not.

    Those three are the known exceptions. A plain error anywhere else fails.
    """
    assert _COMPUTER_TOOLS == "computer_*"
    assert frozenset({"_tool_enable_mcp"}) == _ENABLE_MCP
    assert "load_file" in " ".join(sorted(_SOME_LOAD_FILE))
    violations: list[str] = []
    for path in (_SRC / "monkeybot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_kind_violations(path, tree))
        if path.name == "core_tool_executor.py":
            violations.extend(_shape_violations(path, tree))
    assert not violations, "\n".join(violations)
