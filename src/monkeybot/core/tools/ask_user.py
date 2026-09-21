"""ask_user: a structured question the host answers before the tool returns."""

from __future__ import annotations

from typing import Any

from monkeybot.core.runtime.events import AskUserRequestEvent, event_to_json

_MAX_QUESTION = 2_000
_MAX_CHOICE = 160
_MIN_CHOICES = 2
_MAX_CHOICES = 6


def parse_ask_user_args(args: dict[str, Any]) -> tuple[str, tuple[str, ...]] | str:
    """Return ``(question, choices)`` or an error string."""
    raw_question = args.get("question")
    question = raw_question.strip() if isinstance(raw_question, str) else ""
    if not question:
        return "ask_user requires a non-empty question"
    if len(question) > _MAX_QUESTION:
        question = question[:_MAX_QUESTION]
    raw_choices = args.get("choices")
    if raw_choices is None:
        return question, ()
    if not isinstance(raw_choices, list):
        return "ask_user choices must be a list of strings"
    choices: list[str] = []
    for item in raw_choices:
        if not isinstance(item, str) or not item.strip():
            return "ask_user choices must be non-empty strings"
        text = item.strip()
        if len(text) > _MAX_CHOICE:
            return "ask_user choices must be short"
        if text in choices:
            return "ask_user choices must be unique"
        choices.append(text)
    if len(choices) < _MIN_CHOICES or len(choices) > _MAX_CHOICES:
        return f"ask_user choices must include {_MIN_CHOICES} to {_MAX_CHOICES} options"
    return question, tuple(choices)


async def await_ask_user_answer(
    bus: Any,
    *,
    call_id: str,
    request_id: str,
    question: str,
    choices: tuple[str, ...],
) -> str | None:
    """Publish the question and wait until the host posts an answer.

    Returns the answer, or ``None`` when this session cannot ask.
    ``asyncio.CancelledError`` propagates when the turn is stopped.
    """
    publish = getattr(bus, "publish_data", None)
    register = getattr(bus, "register_pending", None)
    if publish is None or register is None or not call_id:
        return None
    fut = register(call_id)
    await publish(
        event_to_json(
            AskUserRequestEvent(
                request_id=request_id,
                tool_call_id=call_id,
                question=question,
                choices=choices,
            )
        )
    )
    payload = await fut
    if not isinstance(payload, dict):
        return ""
    answer = payload.get("answer")
    return answer.strip() if isinstance(answer, str) else ""
