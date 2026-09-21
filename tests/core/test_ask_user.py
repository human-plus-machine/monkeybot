"""ask_user argument checks and the pending-answer wait."""

from __future__ import annotations

import asyncio
import json

from monkeybot.core.tools.ask_user import await_ask_user_answer, parse_ask_user_args


def test_parse_ask_user_requires_a_question() -> None:
    assert parse_ask_user_args({}) == "ask_user requires a non-empty question"
    assert parse_ask_user_args({"question": "  "}) == "ask_user requires a non-empty question"


def test_parse_ask_user_choices() -> None:
    assert parse_ask_user_args({"question": " When? "}) == ("When?", ())
    assert parse_ask_user_args({"question": "When?", "choices": ["Friday", "Monday"]}) == (
        "When?",
        ("Friday", "Monday"),
    )
    assert parse_ask_user_args({"question": "When?", "choices": ["Friday"]}) == (
        "ask_user choices must include 2 to 6 options"
    )
    assert parse_ask_user_args({"question": "When?", "choices": ["Friday", "Friday"]}) == (
        "ask_user choices must be unique"
    )


def test_await_ask_user_answer_publishes_then_waits() -> None:
    class Bus:
        def __init__(self) -> None:
            self.published: list[str] = []
            self.fut: asyncio.Future[object] | None = None

        def register_pending(self, key: str) -> asyncio.Future[object]:
            assert key == "call-1"
            self.fut = asyncio.get_running_loop().create_future()
            return self.fut

        async def publish_data(self, raw: str) -> None:
            self.published.append(raw)
            assert self.fut is not None
            self.fut.set_result({"answer": " Friday "})

    async def _run() -> None:
        bus = Bus()
        answer = await await_ask_user_answer(
            bus,
            call_id="call-1",
            request_id="req",
            question="Which date?",
            choices=("Friday", "Monday"),
        )
        assert answer == "Friday"
        event = json.loads(bus.published[0])
        assert event["type"] == "AskUserRequest"
        assert event["question"] == "Which date?"
        assert event["choices"] == ["Friday", "Monday"]

    asyncio.run(_run())
