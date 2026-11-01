"""In-process loop port double for SSE route tests."""

from __future__ import annotations

from monkeybot.core.runtime.events import AssistantDelta, Thinking, TurnComplete, UsageTotals
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse.session_bus import SessionRegistry
from monkeybot.gateway.sse.sse import agent_event_to_wire_dict, json_dumps_wire


class FakeLoopPort:
    """Publishes a short canned event sequence then clears busy state."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[Text],
        *,
        provenance: str = "human",
    ) -> None:
        _ = user_content
        bus = self._registry.get(session_id)
        if bus is None:
            return
        await bus.publish_data(
            json_dumps_wire(agent_event_to_wire_dict(Thinking(request_id=request_id)))
        )
        await bus.publish_data(
            json_dumps_wire(
                agent_event_to_wire_dict(
                    AssistantDelta(request_id=request_id, delta="hi"),
                )
            )
        )
        await bus.publish_data(
            json_dumps_wire(
                agent_event_to_wire_dict(
                    TurnComplete(
                        request_id=request_id,
                        usage=UsageTotals(
                            input_tokens=1,
                            output_tokens=2,
                            cached_tokens=0,
                            cost_usd=0.0,
                            duration_ms=1,
                            estimated_prompt_tokens=0,
                        ),
                    ),
                )
            )
        )
        bus.current_request_id = None
