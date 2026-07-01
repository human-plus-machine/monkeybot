"""Inspector that requires user confirmation before starting a scheduled loop."""

from __future__ import annotations

from monkeybot.core.context import TurnContext
from monkeybot.core.tools.inspector import Decision, InspectorToolCall, ToolInspector


class LoopStartInspector:
    """Return ``confirm`` for ``start_loop`` so the user approves the plan before it runs."""

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        if call.name != "start_loop":
            return Decision(kind="allow")
        prompt = call.args.get("prompt")
        interval = call.args.get("interval", "?")
        loop_id = call.args.get("loop_id") or "(auto)"
        session_id = call.args.get("session_id") or "loop-main"
        max_ticks = call.args.get("max_ticks")
        max_runtime = call.args.get("max_runtime")
        preview = str(prompt).strip() if isinstance(prompt, str) else ""
        if len(preview) > 400:
            preview = preview[:400] + "…"
        guards: list[str] = []
        if max_ticks is not None:
            guards.append(f"max_ticks={max_ticks}")
        if max_runtime is not None:
            guards.append(f"max_runtime={max_runtime}")
        guard_text = ", ".join(guards) if guards else "no explicit max_ticks/max_runtime"
        message = (
            f"Start scheduled loop?\n"
            f"- loop_id: {loop_id}\n"
            f"- session: {session_id}\n"
            f"- interval: {interval}\n"
            f"- guards: {guard_text}\n\n"
            f"Plan:\n{preview or '(empty prompt)'}"
        )
        del ctx
        return Decision(kind="confirm", message=message)
