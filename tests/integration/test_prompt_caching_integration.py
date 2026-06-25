"""Phase 6 integration: provider-prompt-caching stories wired end-to-end."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.llm.provider import Done, Message, UsageEvent
from monkeybot.providers.sampling import DEFAULT_MODEL_MAX_TOKENS, DEFAULT_MODEL_TEMPERATURE
from monkeybot.core.prompts.prompt import compose_system_prompt
from monkeybot.core.runtime.events import TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.providers.pricing import estimate_cost
from tests.core.test_loop import FakeHistory, FakeProvider, RecordingExecutor, _ctx
from tests.core.test_prompt import _minimal_ctx


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_split_and_cost_flow_through_loop_to_turn_complete() -> None:
    """Stories 1+2: UsageEvent split fields → loop accumulation → priced cost_usd."""
    prov = FakeProvider(
        [
            [
                UsageEvent(
                    input_tokens=100,
                    output_tokens=50,
                    cached_tokens=900,
                    cache_read_tokens=900,
                    cache_creation_tokens=0,
                ),
                Done(),
            ]
        ]
    )
    hist = FakeHistory()
    ctx = _ctx(model="gpt-5")
    events: list[object] = []
    async for e in run(
        "hello",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(e)
    tc = events[-1]
    assert isinstance(tc, TurnComplete)
    assert tc.usage.cache_read_tokens == 900
    assert tc.usage.cache_creation_tokens == 0
    assert tc.usage.cached_tokens == 900
    expected_cost = estimate_cost(
        "gpt-5",
        100,
        50,
        cache_read_tokens=900,
        cache_creation_tokens=0,
    )
    assert tc.usage.cost_usd == pytest.approx(expected_cost)
    assert tc.usage.cost_usd > 0


@pytest.mark.integration
def test_config_cache_flag_threads_to_provider_constructor() -> None:
    """Stories 1+8: get_provider_config(cache_enabled=) resolves for session model pick."""
    mock_instance = MagicMock()
    with patch(
        "monkeybot.core.config.settings.OpenAIProvider",
        return_value=mock_instance,
    ) as mock_cls:
        from monkeybot.core.config.settings import get_provider_config

        cfg = get_provider_config(
            provider="openai",
            model_name="gpt-5",
            cache_enabled=False,
        )
        assert cfg.provider is mock_instance
        assert cfg.model == "gpt-5"
        mock_cls.assert_called_once_with(
            temperature=DEFAULT_MODEL_TEMPERATURE,
            max_tokens=DEFAULT_MODEL_MAX_TOKENS,
            cache_enabled=False,
        )


@pytest.mark.integration
def test_prompt_stable_prefix_before_current_request() -> None:
    """Story 1: compose_system_prompt keeps volatile task after harness."""
    ctx = _minimal_ctx()
    msgs = [
        Message(role="user", content=[Text(text="Do the thing")]),
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="read_file", args={})],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="read_file",
                    result=[Text(text="ok")],
                    is_error=False,
                )
            ],
        ),
    ]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "## Current request" in out
    assert out.index("## Current request") > out.index("MonkeyBot harness")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_usage_payload_includes_cache_and_cost() -> None:
    """Stories 2+8: session usage JSON exposes cache split and cost_usd."""
    from monkeybot.gateway.sse.routes import create_app
    from monkeybot.gateway.sse.session_bus import SessionRegistry
    from tests.gateway.sse.test_routes import FakeLoopPort

    registry = SessionRegistry()
    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/sessions", json={})
        assert created.status_code == 201
        sid = created.json()["session_id"]
        usage = await client.get(f"/sessions/{sid}/usage")
        assert usage.status_code == 200
        body = usage.json()
        assert "cache_read_tokens" in body
        assert "cache_creation_tokens" in body
        assert "cost_usd" in body
        assert body["cache_read_tokens"] == 0
        assert body["cost_usd"] == 0.0
