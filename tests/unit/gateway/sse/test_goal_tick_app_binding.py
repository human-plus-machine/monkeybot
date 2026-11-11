"""Goal ticks must use the FastAPI app that opened storage.

Desktop dev serves the realtime app. Its lifespan runs SSE startup on that
app, so ``state.storage`` lives there. An unbound loop port falls back to the
SSE module app and raises ``'State' object has no attribute 'storage'``.

regression_of: goal-tick-serving-app
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.gateway.sse.app import GatewayLoopPort, bind_goal_tick_port
from monkeybot.gateway.sse.session_bus import SessionRegistry


class _RightApp(Exception):
    pass


class _WrongApp(Exception):
    pass


@pytest.mark.case("T1-GW-07")
@pytest.mark.asyncio
async def test_goal_tick_reads_the_serving_app_storage() -> None:
    serving = FastAPI()
    chat_registry = SessionRegistry()
    serving.state.registry = chat_registry
    serving_storage = MagicMock()
    serving_storage.history.side_effect = _RightApp
    serving.state.storage = serving_storage

    module_storage = MagicMock()
    module_storage.history.side_effect = _WrongApp
    previous = getattr(gateway_app.app.state, "storage", None)
    gateway_app.app.state.storage = module_storage
    try:
        port, registry = bind_goal_tick_port(serving)
        assert registry is chat_registry
        assert port._serving_app() is serving

        registry.create("sess-goal", agent_md=None, created_at_ms=0)
        with pytest.raises(_RightApp):
            await port.start_turn("sess-goal", "req-1", [Text(text="tick")])

        unbound = GatewayLoopPort(chat_registry)
        with pytest.raises(_WrongApp):
            await unbound.start_turn("sess-goal", "req-2", [Text(text="tick")])
    finally:
        gateway_app.app.state.storage = previous
