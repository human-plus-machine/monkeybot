"""Unit tests for monkeybot.core.subagents.subagent_proto."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.runtime.events import AssistantDelta, Error, Thinking, event_to_json
from monkeybot.core.subagents.subagent_proto import (
    SubagentEnvelope,
    _default_subprocess_exec,
    default_subagent_script,
    normalize_sqlite_db_url,
    resolve_agent_project_root,
    resolve_project_path,
    resolve_subagent_agent_md_path,
    resolve_subagent_script,
    resolve_task_agent_md_path,
    spawn_subagent,
)


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


def test_default_subagent_script_exists() -> None:
    script = default_subagent_script()
    assert script.name == "subagent_worker.py"
    assert script.is_file()
    assert script.parent.name == "subagents"


def test_resolve_subagent_script_uses_bundled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MONKEYBOT_SUBAGENT_SCRIPT", raising=False)
    assert resolve_subagent_script() == default_subagent_script().resolve()


def test_resolve_project_path_relative(tmp_path: Path) -> None:
    cfg = tmp_path / "monkeybot_config"
    cfg.mkdir()
    agent = cfg / "AGENT.md"
    agent.write_text("# bot\n", encoding="utf-8")
    got = resolve_project_path("./monkeybot_config/AGENT.md", tmp_path)
    assert got == agent.resolve()


def test_resolve_subagent_agent_md_prefers_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "monkeybot_config" / "AGENT.md"
    parent.parent.mkdir(parents=True)
    parent.write_text("# parent\n", encoding="utf-8")
    override = tmp_path / "custom.md"
    override.write_text("# custom\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MD", "./monkeybot_config/AGENT.md")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_AGENT_MD", str(override))
    assert resolve_subagent_agent_md_path(tmp_path) == override.resolve()


def test_normalize_sqlite_db_url_relative(tmp_path: Path) -> None:
    url = normalize_sqlite_db_url("sqlite:///data/monkeybot.db", tmp_path)
    assert url == f"sqlite:///{(tmp_path / 'data' / 'monkeybot.db').resolve()}"


def test_resolve_agent_project_root_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))
    assert resolve_agent_project_root() == tmp_path.resolve()


def test_subagent_envelope_roundtrip_with_persona_fields() -> None:
    env = SubagentEnvelope(
        task="do thing",
        context="ctx",
        memory_storage_uri="local:///tmp/m",
        parent_run_id="p1",
        agent_md="/tmp/agents/researcher.md",
        subagent_type="researcher",
    )
    restored = SubagentEnvelope.from_json(env.to_json())
    assert restored == env


def test_resolve_task_agent_md_path_uses_registry(tmp_path: Path) -> None:
    impl = tmp_path / "agents" / "researcher.md"
    impl.parent.mkdir(parents=True)
    impl.write_text("# researcher\n", encoding="utf-8")
    registry = {
        "researcher": SubagentConfig(
            name="researcher",
            description="research",
            skills=[],
            agent_md="./agents/researcher.md",
        )
    }
    got = resolve_task_agent_md_path(
        subagent_type="researcher",
        registry=registry,
        agent_root=tmp_path,
    )
    assert got == impl.resolve()


def test_resolve_task_agent_md_path_unknown_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown subagent_type"):
        resolve_task_agent_md_path(
            subagent_type="missing",
            registry={},
            agent_root=tmp_path,
        )


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


def test_subagent_envelope_empty_memory_uri_roundtrip() -> None:
    env = SubagentEnvelope(
        task="do thing",
        context="ctx",
        memory_storage_uri="",
        parent_run_id="p1",
    )
    restored = SubagentEnvelope.from_json(env.to_json())
    assert restored.memory_storage_uri == ""


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
async def test_spawn_subagent_malformed_line_emits_error_continues(
    tmp_scratch: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rid = "r"
    good = event_to_json(AssistantDelta(request_id=rid, delta="ok"))
    lines = ["not-json", good]
    env = SubagentEnvelope(task="a", context="", memory_storage_uri="local://m", parent_run_id="p")

    async def subprocess_exec(*_a: object, **_k: object) -> FakeProcess:
        return FakeProcess(lines, exit_code=0)

    collected = []
    with caplog.at_level("WARNING", logger="monkeybot.core.subagents.subagent_proto"):
        async for evt in spawn_subagent(
            "s.py",
            env,
            scratch_dir=tmp_scratch,
            subprocess_exec=subprocess_exec,
        ):
            collected.append(evt)
    assert any(isinstance(e, Error) for e in collected)
    assert isinstance(collected[-1], AssistantDelta)
    assert "subagent NDJSON parse error" in caplog.text
    assert "parent_run_id=p" in caplog.text


@pytest.mark.asyncio
async def test_spawn_subagent_nonzero_exit_logs_warning(
    tmp_scratch: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = SubagentEnvelope(task="a", context="", memory_storage_uri="local://m", parent_run_id="p")

    async def subprocess_exec(*_a: object, **_k: object) -> FakeProcess:
        return FakeProcess([], exit_code=7)

    with caplog.at_level("WARNING", logger="monkeybot.core.subagents.subagent_proto"):
        collected = [
            evt
            async for evt in spawn_subagent(
                "s.py",
                env,
                scratch_dir=tmp_scratch,
                subprocess_exec=subprocess_exec,
            )
        ]

    assert any(isinstance(e, Error) and "code 7" in e.error for e in collected)
    assert "subagent process exited nonzero" in caplog.text
    assert "exit_code=7" in caplog.text


@pytest.mark.asyncio
async def test_default_subprocess_exec_discards_stderr_to_avoid_deadlock(tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('x' * 200000)\n"
        "sys.stderr.flush()\n"
        "print('ok', flush=True)\n",
        encoding="utf-8",
    )
    proc = await _default_subprocess_exec(sys.executable, "-u", str(script))
    assert proc.stdout is not None
    assert (await asyncio.wait_for(proc.stdout.readline(), timeout=2)).strip() == b"ok"
    assert await asyncio.wait_for(proc.wait(), timeout=2) == 0
