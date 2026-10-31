"""T1-RT-01. Pin what the realtime loop shares with the text loop, and what it does not.

A refactor that imports a text-loop guard, or that stops calling truncated-batch
rejection, confirm wait, or registry refresh, fails here on purpose. That diff
is reviewed; this file is not updated to follow it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_LOOP = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "monkeybot"
    / "core"
    / "runtime"
    / "realtime_loop.py"
)

# Sibling modules under core/runtime. doom_loop, turn_loop, compaction, and the
# text-loop facade are absent on purpose.
_SIBLINGS: dict[str, frozenset[str]] = {
    "loop_ports": frozenset({"ToolExecutorPort"}),
    "tool_batch": frozenset(
        {
            "_await_user_response_any",
            "_note_registry_mutation",
            "_rejected_tool_batch_error",
            "_should_reject_tool_batch",
            "confirm_wait_stopped_by_user",
        }
    ),
    "tool_dispatch": frozenset({"file_change_events"}),
}

_FORBIDDEN_MODULES = frozenset(
    {
        "monkeybot.core.runtime.doom_loop",
        "monkeybot.core.runtime.turn_loop",
        "monkeybot.core.runtime.history_compaction",
        "monkeybot.core.runtime.loop",
        "monkeybot.core.runtime.input_admission",
        "monkeybot.core.runtime.context_budget",
    }
)

# Text-loop guards the realtime loop does not call. Names only, so a comment
# that mentions them does not fail the pin.
_FORBIDDEN_CALLS = frozenset(
    {
        "_drain_steers",
        "_gate_final_answer",
        "_DoomLoopTracker",
        "_effective_doom_loop_threshold",
        "_doom_loop_exempt_names",
        "compact_history",
        "summarize_history",
    }
)

_SHARED_CALLS = frozenset(
    {
        "_should_reject_tool_batch",
        "_rejected_tool_batch_error",
        "confirm_wait_stopped_by_user",
        "_await_user_response_any",
        "_note_registry_mutation",
        "file_change_events",
        "refresh_tools_after_mcp_change",
        "refresh_tools_after_loops_change",
    }
)

_TRUNCATED_CALL = "_should_reject_tool_batch(pending_calls, truncated=False)"


def _sibling_imports(tree: ast.AST) -> dict[str, frozenset[str]]:
    found: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found[node.module] = frozenset(alias.name for alias in node.names)
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


@pytest.mark.case("T1-RT-01")
def test_realtime_loop_divergence_pinned() -> None:
    source = _LOOP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    siblings = _sibling_imports(tree)
    assert siblings == _SIBLINGS
    forbidden_imports = sorted(_imported_modules(tree) & _FORBIDDEN_MODULES)
    assert forbidden_imports == []
    called = _called_names(tree)
    assert _FORBIDDEN_CALLS.isdisjoint(called)
    missing = sorted(_SHARED_CALLS - called)
    assert missing == []
    assert _TRUNCATED_CALL in source
