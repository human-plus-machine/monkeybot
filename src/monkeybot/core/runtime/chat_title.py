"""Short chat titles from the first user message and the first assistant reply.

A separate, cheap model call. It is not part of the reply the user is waiting on.
``CONTEXT_SUMMARIZATION_MODEL`` is preferred so a local or small model can name
the chat without using the main chat model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import aclosing
from typing import Any, cast

from monkeybot.core.config.settings import _agent_model_name
from monkeybot.core.config.snapshot import current_env
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta
from monkeybot.core.memory.ingest import visible_text
from monkeybot.core.types.content_blocks import Text

CHAT_TITLE_TIMEOUT_SECONDS = 15
TITLE_SOURCE_CHARS = 1500
TITLE_MAX_CHARS = 60

_TITLE_SYSTEM = (
    "Name this chat in 3 to 6 words. Reply with only the title, "
    "with no quotes and no trailing punctuation."
)
_WRAP = "\"'`*_#“”‘’"
_TRAIL = " \t.!?:;,-\"'`*_"


def title_model_id() -> str:
    """Summarization model, else the configured chat model."""
    chosen = current_env("CONTEXT_SUMMARIZATION_MODEL", "").strip()
    if chosen:
        return chosen
    return _agent_model_name(None)


def clip_title_source(text: str, limit: int = TITLE_SOURCE_CHARS) -> str:
    """One line, capped, so a long first message cannot dominate the call."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip()


def clean_title(raw: str) -> str | None:
    """Turn a model reply into a single short title, or None when nothing is left."""
    line = next((part.strip() for part in raw.splitlines() if part.strip()), "")
    if not line:
        return None
    cleaned = re.sub(r"(?i)^title\s*:\s*", "", line).strip()
    changed = True
    while changed and cleaned:
        changed = False
        nxt = cleaned.strip(_WRAP).strip()
        if len(nxt) >= 2 and nxt[0] == nxt[-1] and nxt[0] in "\"'“”":
            nxt = nxt[1:-1].strip()
        if nxt != cleaned:
            cleaned = nxt
            changed = True
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(_TRAIL).strip()
    if len(cleaned) > TITLE_MAX_CHARS:
        cleaned = cleaned[:TITLE_MAX_CHARS].rstrip(_TRAIL).strip()
    return cleaned or None


def opening_turn_texts(messages: Sequence[Message]) -> tuple[str, str]:
    """First user text and the first assistant text that is actually words."""
    user = ""
    assistant = ""
    for message in messages:
        if message.role == "user" and not user:
            user = visible_text(message)
        elif message.role == "assistant" and not assistant:
            assistant = visible_text(message)
        if user and assistant:
            break
    return user, assistant


async def generate_chat_title(
    provider: Provider,
    model: str,
    user_text: str,
    assistant_text: str,
) -> str | None:
    """Ask ``provider`` for a title. None when there is nothing to name or no title comes back."""
    user = clip_title_source(user_text)
    assistant = clip_title_source(assistant_text)
    if not user and not assistant:
        return None
    prompt = f"User message:\n{user}\n\nAssistant reply:\n{assistant}"
    messages = [
        Message(role="system", content=[Text(text=_TITLE_SYSTEM)]),
        Message(role="user", content=[Text(text=prompt)]),
    ]
    collected = ""
    async with aclosing(cast(Any, provider.stream(messages, [], model=model))) as stream:
        async for event in stream:
            if isinstance(event, TextDelta):
                collected += event.text
            elif isinstance(event, Done):
                break
    return clean_title(collected)
