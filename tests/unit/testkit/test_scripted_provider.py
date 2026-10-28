"""Multi-turn strict ScriptedProvider, and the flat fake provider it does not replace."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests
from monkeybot.core.llm.provider import (
    Done,
    GroundingEvent,
    Message,
    ProviderCallHints,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolInputDelta,
    UsageEvent,
)
from monkeybot.core.subagents import subagent_worker
from monkeybot.core.testing.mocks_provider import (
    RaiseStep,
    ScriptedFakeProvider,
    ScriptedProvider,
    ScriptedProviderExhaustedError,
    fake_provider_prompt_tokens,
    load_scripted_provider,
)
from monkeybot.core.types.interfaces import LLMError
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.providers.gemini import GeminiProvider
from testkit.provider import CapturingProvider, FakeProvider, ToolsRecordingProvider

_VOCABULARY_YAML = """\
- - type: text
    text: hello
  - type: thinking
    text: hmm
    signature: sig
  - type: tool_input_delta
    call_id: c1
    name: read_file
    delta: '{"path":'
  - type: tool_call
    call_id: c1
    name: read_file
    args:
      path: /tmp/a
    parse_error: bad
    metadata:
      source: script
  - type: grounding
    sources:
      - uri: https://example.com
        title: Example
    search_queries:
      - example
  - type: usage
    input_tokens: 3
    output_tokens: 4
    cached_tokens: 1
    cache_read_tokens: 1
    cache_creation_tokens: 0
  - type: done
    truncated: true
- - type: text
    text: second
  - type: done
"""

_VOCABULARY_TURNS: list[list[dict[str, object]]] = [
    [
        {"type": "text", "text": "hello"},
        {"type": "thinking", "text": "hmm", "signature": "sig"},
        {"type": "tool_input_delta", "call_id": "c1", "name": "read_file", "delta": '{"path":'},
        {
            "type": "tool_call",
            "call_id": "c1",
            "name": "read_file",
            "args": {"path": "/tmp/a"},
            "parse_error": "bad",
            "metadata": {"source": "script"},
        },
        {
            "type": "grounding",
            "sources": [{"uri": "https://example.com", "title": "Example"}],
            "search_queries": ["example"],
        },
        {
            "type": "usage",
            "input_tokens": 3,
            "output_tokens": 4,
            "cached_tokens": 1,
            "cache_read_tokens": 1,
            "cache_creation_tokens": 0,
        },
        {"type": "done", "truncated": True},
    ],
    [
        {"type": "text", "text": "second"},
        {"type": "done"},
    ],
]

_VOCABULARY_EVENTS: list[ProviderEvent] = [
    TextDelta(text="hello"),
    ThinkingDelta(text="hmm", signature="sig"),
    ToolInputDelta(call_id="c1", name="read_file", delta='{"path":'),
    ToolCall(
        call_id="c1",
        name="read_file",
        args={"path": "/tmp/a"},
        parse_error="bad",
        metadata={"source": "script"},
    ),
    GroundingEvent(
        sources=[{"uri": "https://example.com", "title": "Example"}],
        search_queries=["example"],
    ),
    UsageEvent(
        input_tokens=3,
        output_tokens=4,
        cached_tokens=1,
        cache_read_tokens=1,
        cache_creation_tokens=0,
    ),
    Done(truncated=True),
]


@pytest.fixture
def isolated_provider_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_runtime_env_state_for_tests()
    monkeypatch.delenv("MONKEYBOT_FAKE_PROVIDER_SCRIPT", raising=False)
    monkeypatch.delenv("MONKEYBOT_FAKE_PROVIDER_EVENTS", raising=False)
    yield
    reset_runtime_env_state_for_tests()


async def _collect(
    provider: ScriptedProvider | ScriptedFakeProvider,
    *,
    model: str = "m",
) -> list[ProviderEvent]:
    return [ev async for ev in provider.stream([], [], model=model)]


async def test_second_stream_call_gets_the_second_turn() -> None:
    provider = ScriptedProvider(
        [
            [TextDelta(text="one"), Done()],
            [TextDelta(text="two"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
        ]
    )
    assert provider.name == "fake"
    assert provider.supports_streaming is True
    assert await _collect(provider) == [TextDelta(text="one"), Done()]
    assert await _collect(provider) == [
        TextDelta(text="two"),
        UsageEvent(input_tokens=1, output_tokens=1),
        Done(),
    ]


async def test_strict_overrun_raises_instead_of_yielding_nothing() -> None:
    provider = ScriptedProvider([[TextDelta(text="only")]])
    assert await _collect(provider) == [TextDelta(text="only")]

    overrun = provider.stream([], [], model="m")
    with pytest.raises(ScriptedProviderExhaustedError, match=r"strict mode: stream\(\) call 2"):
        await overrun.__anext__()
    assert provider.stream_calls == 2

    empty = ScriptedProvider([])
    with pytest.raises(ScriptedProviderExhaustedError, match=r"0 turn\(s\)"):
        await empty.stream([], [], model="m").__anext__()


async def test_raise_step_raises_when_reached() -> None:
    provider = ScriptedProvider([[RaiseStep(error=RuntimeError, message="boom")]])
    seen: list[ProviderEvent] = []
    with pytest.raises(RuntimeError, match="boom"):
        async for ev in provider.stream([], [], model="m"):
            seen.append(ev)
    assert seen == []


async def test_raise_step_after_n_events() -> None:
    provider = ScriptedProvider(
        [
            [
                TextDelta(text="a"),
                TextDelta(text="b"),
                TextDelta(text="c"),
                RaiseStep(error=ConnectionError, message="dropped", after=2),
            ],
            [TextDelta(text="later"), Done()],
        ]
    )
    seen: list[ProviderEvent] = []
    with pytest.raises(ConnectionError, match="dropped"):
        async for ev in provider.stream([], [], model="m"):
            seen.append(ev)
    assert seen == [TextDelta(text="a"), TextDelta(text="b")]
    assert await _collect(provider) == [TextDelta(text="later"), Done()]


async def test_records_stream_calls_and_count_input_tokens() -> None:
    hints = ProviderCallHints(session_id="s", cache_retention="long")
    tool = ToolDef(name="read_file", description="read", input_schema={"type": "object"})
    first = [Message.text("user", "hi")]
    second = [Message.text("user", "again")]
    provider = ScriptedProvider(
        [
            [TextDelta(text="one"), Done()],
            [TextDelta(text="two"), Done()],
        ]
    )

    counted = await provider.count_input_tokens(
        first,
        [tool],
        model="count-model",
        thinking_budget=3,
        hints=hints,
    )
    assert counted == fake_provider_prompt_tokens(first, [tool])
    assert len(provider.count_input_tokens_calls) == 1
    call = provider.count_input_tokens_calls[0]
    assert call.messages == tuple(first)
    assert call.tools == (tool,)
    assert call.model == "count-model"
    assert call.thinking_budget == 3
    assert call.hints == hints
    assert provider.stream_calls == 0

    assert [ev async for ev in provider.stream(first, [tool], model="m1", hints=hints)] == [
        TextDelta(text="one"),
        Done(),
    ]
    assert [ev async for ev in provider.stream(second, [], model="m2")] == [
        TextDelta(text="two"),
        Done(),
    ]
    assert provider.stream_calls == 2
    assert provider.stream_messages == [first, second]
    assert [[item.name for item in tools] for tools in provider.stream_tools] == [
        ["read_file"],
        [],
    ]
    assert provider.stream_models == ["m1", "m2"]
    assert provider.stream_hints == [hints, None]
    assert len(provider.count_input_tokens_calls) == 1


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
async def test_script_file_round_trips_provider_events(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"script{suffix}"
    if suffix == ".yaml":
        path.write_text(_VOCABULARY_YAML, encoding="utf-8")
    else:
        path.write_text(json.dumps(_VOCABULARY_TURNS), encoding="utf-8")
    provider = load_scripted_provider(path)
    assert await _collect(provider) == _VOCABULARY_EVENTS
    assert await _collect(provider) == [TextDelta(text="second"), Done()]


async def test_json_raise_accepts_a_dotted_exception_path(tmp_path: Path) -> None:
    path = tmp_path / "raise.json"
    path.write_text(
        json.dumps(
            [
                [
                    {
                        "type": "raise",
                        "error": "monkeybot.core.types.interfaces.LLMError",
                        "message": "nope",
                        "after": 0,
                    },
                    {"type": "text", "text": "hidden"},
                ]
            ]
        ),
        encoding="utf-8",
    )
    provider = load_scripted_provider(path)
    seen: list[ProviderEvent] = []
    with pytest.raises(LLMError, match="nope"):
        async for ev in provider.stream([], [], model="m"):
            seen.append(ev)
    assert seen == []


def test_unknown_event_type_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([[{"type": "text_delta", "text": "no"}]]), encoding="utf-8")
    with pytest.raises(ValueError, match="text_delta"):
        load_scripted_provider(path)


def test_missing_script_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="MONKEYBOT_FAKE_PROVIDER_SCRIPT"):
        load_scripted_provider(tmp_path / "missing.yaml")


async def test_gateway_fake_script_yaml(
    isolated_provider_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del isolated_provider_env
    path = tmp_path / "turns.yaml"
    path.write_text(
        "- - type: text\n    text: from-yaml\n  - type: done\n"
        "- - type: text\n    text: yaml-2\n  - type: done\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("MONKEYBOT_FAKE_PROVIDER_SCRIPT", str(path))
    monkeypatch.setenv(
        "MONKEYBOT_FAKE_PROVIDER_EVENTS",
        json.dumps([[{"kind": "text_delta", "text": "from-events"}, {"kind": "done"}]]),
    )
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, ScriptedProvider)
    assert not isinstance(provider, ScriptedFakeProvider)
    assert await _collect(provider) == [TextDelta(text="from-yaml"), Done()]
    assert await _collect(provider) == [TextDelta(text="yaml-2"), Done()]


async def test_subagent_fake_script_json(
    isolated_provider_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del isolated_provider_env
    path = tmp_path / "turns.json"
    path.write_text(
        json.dumps(
            [
                [{"type": "text", "text": "from-json"}, {"type": "done", "truncated": True}],
                [{"type": "raise", "error": "RuntimeError", "message": "json-boom", "after": 0}],
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("MONKEYBOT_FAKE_PROVIDER_SCRIPT", str(path))
    provider = subagent_worker._resolve_provider()
    assert isinstance(provider, ScriptedProvider)
    assert not isinstance(provider, ScriptedFakeProvider)
    assert await _collect(provider) == [TextDelta(text="from-json"), Done(truncated=True)]
    seen: list[ProviderEvent] = []
    with pytest.raises(RuntimeError, match="json-boom"):
        async for ev in provider.stream([], [], model="m"):
            seen.append(ev)
    assert seen == []


async def test_unset_script_keeps_flat_fake_provider(
    isolated_provider_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_provider_env
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    gateway = gateway_app._resolve_provider()
    assert isinstance(gateway, ScriptedFakeProvider)
    assert not isinstance(gateway, ScriptedProvider)
    hello = [
        TextDelta(text="hello"),
        UsageEvent(input_tokens=1, output_tokens=2, cached_tokens=0),
        Done(),
    ]
    assert await _collect(gateway) == hello
    assert await _collect(gateway) == hello

    subagent = subagent_worker._resolve_provider()
    assert isinstance(subagent, ScriptedFakeProvider)
    assert not isinstance(subagent, ScriptedProvider)
    sub_events = [
        TextDelta(
            text="subagent fake provider: set MONKEYBOT_FAKE_PROVIDER_EVENTS for scripted tools."
        ),
        UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0),
        Done(),
    ]
    assert await _collect(subagent) == sub_events
    assert await _collect(subagent) == sub_events


async def test_events_json_stays_flat_and_repeats(
    isolated_provider_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_provider_env
    payload = [
        [{"kind": "text_delta", "text": "one"}, {"kind": "done"}],
        [
            {"kind": "text_delta", "text": "two"},
            {"kind": "tool_call", "call_id": "c", "name": "read_file", "args": {"a": 1}},
            {"kind": "usage", "input_tokens": 3, "output_tokens": 4, "cached_tokens": 5},
            {"kind": "done"},
        ],
    ]
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("MONKEYBOT_FAKE_PROVIDER_EVENTS", json.dumps(payload))
    expected = [
        TextDelta(text="one"),
        Done(),
        TextDelta(text="two"),
        ToolCall(call_id="c", name="read_file", args={"a": 1}),
        UsageEvent(input_tokens=3, output_tokens=4, cached_tokens=5),
        Done(),
    ]
    gateway = gateway_app._resolve_provider()
    assert isinstance(gateway, ScriptedFakeProvider)
    assert not isinstance(gateway, ScriptedProvider)
    assert await _collect(gateway) == expected
    assert await _collect(gateway) == expected

    subagent = subagent_worker._resolve_provider()
    assert isinstance(subagent, ScriptedFakeProvider)
    assert await _collect(subagent) == expected
    assert await _collect(subagent) == expected


async def test_scripted_fake_provider_repeats_every_call() -> None:
    provider = ScriptedFakeProvider([TextDelta(text="a"), Done()])
    assert isinstance(provider, ScriptedFakeProvider)
    assert not isinstance(provider, ScriptedProvider)
    assert await _collect(provider) == [TextDelta(text="a"), Done()]
    assert await _collect(provider) == [TextDelta(text="a"), Done()]


async def test_testkit_fakes_keep_their_behavior() -> None:
    import testkit.provider as testkit_provider

    assert testkit_provider.ScriptedProvider is ScriptedProvider

    fake = FakeProvider([[TextDelta(text="a")]])
    assert [ev async for ev in fake.stream([], [], model="m")] == [TextDelta(text="a")]
    assert [ev async for ev in fake.stream([], [], model="m")] == []

    tool = ToolDef(name="read_file", description="read", input_schema={})
    capturing = CapturingProvider([[TextDelta(text="z"), Done()]])
    assert [
        ev async for ev in capturing.stream([Message.text("system", "sys")], [tool], model="m")
    ] == [
        TextDelta(text="z"),
        Done(),
    ]
    assert capturing.system_texts == ["sys"]
    assert [ev async for ev in capturing.stream([], [], model="m")] == []

    recording = ToolsRecordingProvider([[Done()]])
    assert [ev async for ev in recording.stream([], [tool], model="m")] == [Done()]
    assert recording.stream_tools == [["read_file"]]
    assert [ev async for ev in recording.stream([], [], model="m")] == []


def test_script_env_does_not_affect_other_providers(
    isolated_provider_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del isolated_provider_env
    path = tmp_path / "turns.yaml"
    path.write_text("- - type: text\n    text: hi\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_FAKE_PROVIDER_SCRIPT", str(path))
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, GeminiProvider)
    assert not isinstance(provider, ScriptedProvider)
