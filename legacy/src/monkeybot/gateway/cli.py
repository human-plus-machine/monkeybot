"""CLI gateway — reads stdin, drives AgentLoop, prints events to stdout."""
from __future__ import annotations

import asyncio
import sys

from monkeybot.core.events import (
    AssistantDelta,
    ErrorEvent,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
)
from monkeybot.core.loop import AgentLoop


class CLIGateway:
    """Interactive CLI gateway for AgentLoop."""

    def __init__(self, loop: AgentLoop, session_id: str) -> None:
        self._loop = loop
        self._session_id = session_id

    async def run_interactive(self) -> None:
        """Read-eval-print loop until 'exit' or EOF."""
        print("MonkeyBot ready. Type 'exit' to quit.\n")
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            if user_input.strip().lower() == "exit":
                break
            if not user_input.strip():
                continue

            async for event in self._loop.run(user_input, self._session_id):
                if isinstance(event, AssistantDelta):
                    print(event.text, end="", flush=True)
                elif isinstance(event, ToolCallStarted):
                    args_preview = str(event.args)[:80]
                    print(f"\n[Tool: {event.tool_name}({args_preview})]")
                elif isinstance(event, ToolCallResult):
                    preview = (event.result or event.error or "")[:100]
                    print(f"[Result: {preview}]")
                elif isinstance(event, TurnComplete):
                    print(
                        f"\n[{event.input_tokens}in/{event.output_tokens}out tokens, "
                        f"{event.duration_ms}ms]"
                    )
                elif isinstance(event, ErrorEvent):
                    print(f"\nError: {event.message}", file=sys.stderr)
