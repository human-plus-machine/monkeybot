"""Eval hook: collects loop-visible signals without changing harness behavior."""

from __future__ import annotations

from dataclasses import dataclass, field

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload


@dataclass
class EvalRecord:
    """Populated by :class:`EvalHook` and finalized by :mod:`scenario_runner`."""

    tool_calls: list[str] = field(default_factory=list)
    memory_injected_lines: list[str] = field(default_factory=list)
    turn_count: int = 0
    completed: bool = False
    trace_id: str | None = None
    errors: list[str] = field(default_factory=list)


class EvalHook:
    """Registers PRE_TURN / POST_TURN handlers on a :class:`HookManager`.

    ``POST_TOOL`` hooks are fire-and-forget in the loop (``timeout_s=0``) and may
    not finish before ``run`` returns; callers should copy tool names from
    :class:`~tests.core.test_loop.RecordingExecutor`.calls after the run.
    """

    def __init__(self, record: EvalRecord | None = None) -> None:
        self.record = record if record is not None else EvalRecord()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.PRE_TURN, self._on_pre_turn)
        manager.register(HookEvent.POST_TURN, self._on_post_turn)

    async def _on_pre_turn(self, p: HookPayload) -> None:
        if p.inject_memory_lines:
            self.record.memory_injected_lines.extend(p.inject_memory_lines)

    async def _on_post_turn(self, p: HookPayload) -> None:
        del p
        self.record.turn_count += 1
