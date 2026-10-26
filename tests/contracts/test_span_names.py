"""T1-OPS-02. Span names and the attributes each opener always sets."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SPANS = Path(__file__).resolve().parents[2] / "src" / "monkeybot" / "observability" / "spans.py"
_GOLDEN = Path(__file__).resolve().parent / "goldens" / "span_names.txt"
_OPENERS = frozenset(
    {"span_run", "begin_turn_span", "span_llm", "span_tool", "span_subagent", "span_summarize"}
)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _unconditional(stmts: list[ast.stmt]) -> list[ast.stmt]:
    found: list[ast.stmt] = []
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            continue
        found.append(stmt)
        body: list[ast.stmt] | None = None
        if isinstance(stmt, (ast.With, ast.AsyncWith, ast.Try)):
            body = list(stmt.body)
        if body is not None:
            found.extend(_unconditional(body))
    return found


def _span_name(fn: ast.AST) -> str | None:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or _call_name(node) not in {
            "start_as_current_span",
            "start_span",
        }:
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
    return None


def _mirror_keys(call: ast.Call) -> set[str]:
    keys: set[str] = set()
    trace_level = False
    has_input = False
    has_output = False
    for keyword in call.keywords:
        if keyword.arg == "trace_level" and isinstance(keyword.value, ast.Constant):
            trace_level = keyword.value.value is True
        if keyword.arg == "input_value" and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        ):
            has_input = True
        if keyword.arg == "output_value" and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        ):
            has_output = True
    if has_input:
        keys.update({"langfuse.observation.input", "input.value"})
        if trace_level:
            keys.add("langfuse.trace.input")
    if has_output:
        keys.update({"langfuse.observation.output", "output.value"})
        if trace_level:
            keys.add("langfuse.trace.output")
    return keys


def _attributes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    keys: set[str] = set()
    for stmt in _unconditional(fn.body):
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        name = _call_name(call)
        if name == "set_span_attribute_safe" and len(call.args) >= 2:
            key = call.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
        elif name == "_set_openinference_span_kind":
            keys.add("openinference.span.kind")
        elif name == "_mirror_observation_io":
            keys.update(_mirror_keys(call))
    return keys


def _contract() -> str:
    tree = ast.parse(_SPANS.read_text(encoding="utf-8"), filename=str(_SPANS))
    blocks: list[tuple[str, set[str]]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in _OPENERS:
            continue
        span = _span_name(node)
        if span is None:
            continue
        blocks.append((span, _attributes(node)))
    lines: list[str] = []
    for span, keys in sorted(blocks):
        lines.append(span)
        lines.extend(f"  {key}" for key in sorted(keys))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _commit(text: str, *, update: bool) -> None:
    if update:
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(text, encoding="utf-8")
        return
    assert _GOLDEN.is_file(), "missing span_names.txt; re-run with --update-golden"
    assert _GOLDEN.read_text(encoding="utf-8") == text


@pytest.mark.case("T1-OPS-02")
def test_span_names_match_committed_list(request: pytest.FixtureRequest) -> None:
    """Span names and required attributes for run/turn/llm/tool/summarize/subagent."""
    text = _contract()
    names = {line.strip() for line in text.splitlines() if line and not line.startswith(" ")}
    assert names == {
        "monkeybot.run",
        "monkeybot.turn",
        "monkeybot.llm.stream",
        "monkeybot.tool",
        "monkeybot.subagent",
        "monkeybot.context.summarize",
    }
    _commit(text, update=bool(request.config.getoption("update_golden")))
