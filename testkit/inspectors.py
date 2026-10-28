"""Allow-all tool inspector shared by loop tests."""

from __future__ import annotations

from monkeybot.core.tools.inspector import Decision


class AllowInspector:
    async def check(self, call: object, ctx: object) -> Decision:
        del call, ctx
        return Decision(kind="allow")
