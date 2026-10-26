"""Bundle contract for the loop driver.

A bundle owns one slice of scenario setup. ``prepare`` runs before the turn,
``register`` adds hooks, and ``close`` releases what that bundle opened.
Per-run state lives on :class:`RunCtx`, never on the bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from monkeybot.core.config.snapshot import RuntimeConfig
from monkeybot.core.hooks import HookManager
from regression.schema.scenario import Scenario


class LoopDriverError(RuntimeError):
    """The loop driver cannot run this scenario as written."""


@dataclass
class RunCtx:
    """Locals :func:`regression.drivers.loop._run` used to keep on the stack."""

    scenario: Scenario
    workdir: Path
    cfg: RuntimeConfig
    env: dict[str, str]
    merged: dict[str, Any]
    config_path: Path
    hook_names: list[str]
    gate: bool
    workspace: Path | None = None
    pristine: Path | None = None
    hook_memory: Any = None
    read_ledger: Any = None
    mcp_client: Any = None
    mcp_path: Path | None = None
    advertised: list[Any] | None = None
    catalog: tuple[str, ...] = ()
    executor: Any = None
    backend: Any = None
    loop_store: Any = None
    todo_store: Any = None
    extra_tool_defs: list[Any] = field(default_factory=list)
    verifier: Any = None
    task_ledger: Any = None
    policy: Any = None
    messages: list[str] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)
    record: Any = None
    scanner_mode: str | None = None
    goal_ledger: Any = None
    project_instructions: str = ""
    repo_overview: str = ""
    session_id: str = "loop-scenario"
    boot_error: str | None = None
    outbox_count: int | None = None
    usage_backend: Any = None
    attachment_store: Any = None
    host_tools: list[Any] = field(default_factory=list)
    computer_inspector: Any = None
    approvals_persist: Any = None
    huge_trash_root: Path | None = None


class Bundle:
    """One discovered unit. Subclasses set ``order`` and ``hooks``."""

    order: int = 0
    hooks: tuple[str, ...] = ()
    # True registers after EvalHook, matching the previous verifier and task ledger.
    after_eval: bool = False

    async def prepare(self, ctx: RunCtx) -> None:
        return None

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        del manager, ctx
        return None

    async def close(self, ctx: RunCtx) -> None:
        del ctx
        return None
