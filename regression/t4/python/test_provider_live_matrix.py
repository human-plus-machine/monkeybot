"""T4-PROV-01: one live ``stream`` with a trivial tool, plus ``count_input_tokens``, per provider.

Ollama Cloud always runs, on the agent's model; a missing key fails the test. Every other
provider runs only when its key is set, and otherwise skips with the reason. Providers have no
default model id, so a provider with a key also needs ``MONKEYBOT_LIVE_MATRIX_MODEL_<ID>``.
Keys come from the environment or, failing that, the repo-root ``.env``. They are never printed.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest

from regression.t4 import preflight

pytestmark = pytest.mark.live

_TIMEOUT_SEC = 60.0
_MAX_TOKENS = 256
_PROMPT = "Call the echo tool once with text set to 'ping'."
_TOOL_NAME = "echo"


@dataclass(frozen=True)
class _Target:
    provider: str
    key_var: str

    @property
    def model_var(self) -> str:
        return "MONKEYBOT_LIVE_MATRIX_MODEL_" + self.provider.upper().replace("-", "_")


_OLLAMA = _Target(preflight.PROVIDER, preflight.KEY_VAR)
_TARGETS = (
    _OLLAMA,
    _Target("openai", "OPENAI_API_KEY"),
    _Target("anthropic", "ANTHROPIC_API_KEY"),
    _Target("google_genai", "GEMINI_API_KEY"),
    _Target("google_vertexai", "GOOGLE_APPLICATION_CREDENTIALS"),
    _Target("vertex_anthropic", "GOOGLE_APPLICATION_CREDENTIALS"),
    _Target("aws_bedrock", "AWS_ACCESS_KEY_ID"),
    _Target("nvidia", "NVIDIA_API_KEY"),
    _Target("openrouter", "OPENROUTER_API_KEY"),
    _Target("fireworks", "FIREWORKS_API_KEY"),
    _Target("huggingface", "HF_TOKEN"),
)


def _setting(name: str) -> str:
    """``name`` from the environment, else from the repo-root ``.env`` (name matched case-insensitively)."""
    value = (os.environ.get(name) or "").strip()
    if value or not preflight.ENV_FILE.is_file():
        return value
    from dotenv import dotenv_values

    for key, raw in dotenv_values(preflight.ENV_FILE).items():
        if key.strip().upper() == name and (raw or "").strip():
            return (raw or "").strip()
    return ""


def _sequence_problems(events: list[object]) -> list[str]:
    from monkeybot.core.llm.provider import (
        Done,
        GroundingEvent,
        TextDelta,
        ThinkingDelta,
        ToolCall,
        ToolInputDelta,
        UsageEvent,
    )

    known = (TextDelta, ThinkingDelta, ToolCall, ToolInputDelta, GroundingEvent, UsageEvent, Done)
    problems = [
        f"event {index} is {type(event).__name__}, not a ProviderEvent"
        for index, event in enumerate(events)
        if not isinstance(event, known)
    ]
    done_at = [index for index, event in enumerate(events) if isinstance(event, Done)]
    if done_at != [len(events) - 1]:
        problems.append(f"expected one Done as the last of {len(events)} events, saw {done_at}")
    for index, event in enumerate(events):
        if isinstance(event, TextDelta | ThinkingDelta) and not isinstance(event.text, str):
            problems.append(f"event {index}: text is {type(event.text).__name__}")
        elif isinstance(event, ToolCall):
            if event.name != _TOOL_NAME or not event.call_id or not isinstance(event.args, dict):
                problems.append(
                    f"event {index}: tool call {event.name!r} id {event.call_id!r} "
                    f"args {type(event.args).__name__}"
                )
        elif isinstance(event, UsageEvent):
            counts = (
                event.input_tokens,
                event.output_tokens,
                event.cached_tokens,
                event.cache_read_tokens,
                event.cache_creation_tokens,
            )
            if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
                problems.append(f"event {index}: usage counts {counts}")
    return problems


@pytest.mark.case("T4-PROV-01")
@pytest.mark.parametrize("target", _TARGETS, ids=lambda target: target.provider)
async def test_provider_live_matrix(target: _Target, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.config.settings import get_provider_config
    from monkeybot.core.llm.provider import (
        Message,
        provider_count_input_tokens,
        provider_stream,
    )
    from monkeybot.core.types.types_tools import ToolDef

    if target is _OLLAMA:
        if not preflight.ensure_key():
            pytest.fail(preflight.MISSING_KEY_MESSAGE)
        model = preflight.agent_model()[1]
    else:
        key = _setting(target.key_var)
        if not key:
            pytest.skip(f"{target.key_var} is not set")
        model = _setting(target.model_var)
        if not model:
            pytest.skip(f"{target.key_var} is set but {target.model_var} names no model")
        monkeypatch.setenv(target.key_var, key)

    resolved = get_provider_config(
        provider=target.provider, model_name=model, max_tokens=_MAX_TOKENS
    )
    messages = [Message.text("user", _PROMPT)]
    tools = [
        ToolDef(
            name=_TOOL_NAME,
            description="Echo the given text back.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
    ]

    async def collect() -> list[object]:
        return [
            event
            async for event in provider_stream(
                resolved.provider, messages, tools, model=resolved.model
            )
        ]

    events = await asyncio.wait_for(collect(), _TIMEOUT_SEC)
    problems = _sequence_problems(events)
    assert not problems, problems

    count = await asyncio.wait_for(
        provider_count_input_tokens(resolved.provider, messages, tools, model=resolved.model),
        _TIMEOUT_SEC,
    )
    assert isinstance(count, int) and not isinstance(count, bool) and count > 0, count
