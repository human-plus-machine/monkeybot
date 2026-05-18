"""Unit tests for monkeybot.core.subagents.subagent_proto."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from monkeybot.core.runtime.events import AssistantDelta, Error, Thinking, event_to_json
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope, spawn_subagent


class FakeStdin:
    """Capture subprocess stdin writes."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeStdout:
    """Async readline over scripted NDJSON lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._i = 0

    async def readline(self) -> bytes:
        if self._i >= len(self._lines):
            return b""
        raw = self._lines[self._i]
        self._i += 1
        return (raw + "\n").encode("utf-8")

    async def aclose(self) -> None:
        return None


class FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(lines)
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code


def test_subagent_envelope_roundtrip() -> None:
    env = SubagentEnvelope(
        task="do thing",
        context="ctx",
        memory_storage_uri="local:///tmp/m",
        parent_run_id="p1",
        model="m1",
    )
    restored = SubagentEnvelope.from_json(env.to_json())
    assert restored == env


def test_subagent_envelope_roundtrip_with_traceparent() -> None:
    traceparent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    env = SubagentEnvelope(
        task="do thing",
        context="ctx",
        memory_storage_uri="local:///tmp/m",
        parent_run_id="p1",
        model="m1",
        traceparent=traceparent,
    )
    restored = SubagentEnvelope.from_json(env.to_json())
    assert restored == env
    assert restored.traceparent == traceparent


def test_subagent_envelope_roundtrip_without_traceparent() -> None:
    env = SubagentEnvelope(
        task="do thing",
        context="ctx",
        memory_storage_uri="local:///tmp/m",
        parent_run_id="p1",
    )
    payload = json.loads(env.to_json())
    assert "traceparent" not in payload
    restored = SubagentEnvelope.from_json(env.to_json())
    assert restored.traceparent is None


def test_subagent_envelope_rejects_non_string_traceparent() -> None:
    raw = json.dumps(
        {
            "task": "t",
            "context": "",
            "memory_storage_uri": "local://m",
            "parent_run_id": "p",
            "traceparent": 1,
        }
    )
    with pytest.raises(ValueError, match="traceparent"):
        SubagentEnvelope.from_json(raw)


@pytest.fixture
def tmp_scratch(tmp_path: Path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


@pytest.mark.asyncio
async def test_spawn_subagent_writes_progress_and_streams_events(tmp_scratch: Path) -> None:
    rid = "req-1"
    lines = [
        event_to_json(Thinking(request_id=rid)),
        event_to_json(AssistantDelta(request_id=rid, delta="hi")),
    ]
    env = SubagentEnvelope(
        task="t",
        context="c",
        memory_storage_uri="local://m",
        parent_run_id="p",
    )

    holder: dict[str, FakeProcess] = {}

    async def subprocess_exec(*_args: object, **_kw: object) -> FakeProcess:
        proc = FakeProcess(lines, exit_code=0)
        holder["p"] = proc
        return proc

    collected = []
    async for evt in spawn_subagent(
        "child.py",
        env,
        scratch_dir=tmp_scratch,
        subprocess_exec=subprocess_exec,
    ):
        collected.append(evt)

    assert len(collected) == 2
    assert isinstance(collected[0], Thinking)
    assert isinstance(collected[1], AssistantDelta)
    assert collected[1].delta == "hi"

    progress = (tmp_scratch / "progress.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(progress) >= 2

    stdin_bytes = b"".join(holder["p"].stdin.chunks)
    assert SubagentEnvelope.from_json(stdin_bytes.decode("utf-8")) == env

    out = (tmp_scratch / "output.json").read_text(encoding="utf-8")
    assert "AssistantDelta" in out


@pytest.mark.asyncio
async def test_spawn_subagent_on_event_called(tmp_scratch: Path) -> None:
    rid = "r"
    lines = [event_to_json(AssistantDelta(request_id=rid, delta="one"))]
    env = SubagentEnvelope(task="a", context="", memory_storage_uri="local://m", parent_run_id="p")

    async def subprocess_exec(*_a: object, **_k: object) -> FakeProcess:
        return FakeProcess(lines, exit_code=0)

    hook = AsyncMock()
    count = 0
    async for _ in spawn_subagent(
        "s.py",
        env,
        scratch_dir=tmp_scratch,
        on_event=hook,
        subprocess_exec=subprocess_exec,
    ):
        count += 1
    assert count == 1
    hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_subagent_malformed_line_emits_error_continues(tmp_scratch: Path) -> None:
    rid = "r"
    good = event_to_json(AssistantDelta(request_id=rid, delta="ok"))
    lines = ["not-json", good]
    env = SubagentEnvelope(task="a", context="", memory_storage_uri="local://m", parent_run_id="p")

    async def subprocess_exec(*_a: object, **_k: object) -> FakeProcess:
        return FakeProcess(lines, exit_code=0)

    collected = []
    async for evt in spawn_subagent(
        "s.py",
        env,
        scratch_dir=tmp_scratch,
        subprocess_exec=subprocess_exec,
    ):
        collected.append(evt)
    assert any(isinstance(e, Error) for e in collected)
    assert isinstance(collected[-1], AssistantDelta)
