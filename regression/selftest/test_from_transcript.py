"""``from_transcript``, the cassette model, and ``scripted_from_cassette``.

Transcripts here come from the real ``TranscriptWriter``, so the stub pointers
(``schema_seq``, ``text_seq``, ``result_seq``, ``base_seq`` + ``diff``) are the
ones the product writes, not hand-built look-alikes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta, ToolCall, UsageEvent
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import (
    HarnessIntervention,
    SystemPromptSnapshot,
    ThinkingBlockComplete,
    ToolCallResult,
    VerifierVerdict,
)
from monkeybot.core.testing.mocks_provider import ScriptedProviderExhaustedError
from regression.drivers.replay import scripted_scenario
from regression.schema.scenario import Scenario
from regression.t3.tools.cassette import (
    Cassette,
    CassetteCall,
    digest_warnings,
    dump_cassette,
    load_cassette,
    prompt_digest,
    recording_requested,
    stable_prompt_prefix,
    stale_tool,
    tool_schema_digest,
)
from regression.t3.tools.from_transcript import (
    TranscriptConversionError,
    from_transcript,
    main,
)
from testkit.provider import scripted_from_cassette
from testkit.verifier import CLASSIFIER_ROWS_ENV, JUDGE_SCRIPT_ENV

_TOOLS: list[dict[str, object]] = [
    {
        "name": "read_file",
        "description": "Read a file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "glob",
        "description": "Find files.",
        "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
    },
]


def _system_text(workspace: str) -> str:
    filler = "\n".join(f"- guidance line {n}" for n in range(60))
    return (
        "# Agent\n\n## monkeybot harness (fixed)\n"
        f"{filler}\n\n### Runtime paths\n- workspace root: `{workspace}`\n\n"
        "### Skills\n- none\n\n## Current date\n2026-10-26"
    )


def _user(text: str) -> dict[str, object]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


async def _write_session(
    root: Path,
    *,
    secret: str | None = None,
    truncated: bool = False,
    probe: bool = False,
    verdict: bool = False,
) -> Path:
    """Two inner turns: a thinking + ``read_file`` call, then a text answer."""
    writer = TranscriptWriter("s1", workspace_root=root, session_dir=root / "session")
    await writer.ensure_manifest(model="glm-test", provider="ollama-cloud")
    await writer.write_user_message(request_id="r1", content="read a.py")
    first = _system_text("/tmp/one")
    system = {"role": "system", "content": [{"type": "text", "text": first}]}
    await writer.write_event(SystemPromptSnapshot(request_id="r1", inner_turn=1, text=first))
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=1,
        model="glm-test",
        messages=[system, _user("read a.py")],
        tools=_TOOLS,
        thinking_budget=None,
    )
    await writer.write_event(ThinkingBlockComplete(request_id="r1", signature="sig-1"))
    await writer.write_provider_response(
        request_id="r1",
        inner_turn=1,
        model="glm-test",
        text="",
        thinking="open the file",
        tool_requests=[{"call_id": "c1", "name": "read_file", "args": {"path": "a.py"}}],
        usage={"input_tokens": 100, "output_tokens": 9},
    )
    if truncated:
        await writer.write_event(
            HarnessIntervention(
                request_id="r1",
                intervention="truncated_batch",
                inner_turn=1,
                detail="stream_truncated",
            )
        )
    await writer.write_event(
        ToolCallResult(request_id="r1", tool="read_file", result='{"ok": true}', call_id="c1")
    )
    second = _system_text("/tmp/two")
    await writer.write_event(SystemPromptSnapshot(request_id="r1", inner_turn=2, text=second))
    big = "the file body " * 40
    await writer.write_provider_request(
        request_id="r1",
        inner_turn=2,
        model="glm-test",
        messages=[
            {"role": "assistant", "content": [{"type": "text", "text": big}]},
            _user("go on"),
        ],
        message_offset=2,
        tools=_TOOLS,
        thinking_budget=None,
    )
    answer = f"All done. {secret}" if secret else "All done."
    await writer.write_provider_response(
        request_id="r1",
        inner_turn=2,
        model="glm-test",
        text=answer,
        thinking="",
        tool_requests=[],
        usage={"input_tokens": 120, "output_tokens": 4, "cache_read_tokens": 80},
    )
    if probe:
        await writer.write_user_message(request_id="lock-check", content="lock check")
        await writer.write_provider_request(
            request_id="lock-check",
            inner_turn=1,
            model="glm-test",
            messages=[_user("lock check")],
            tools=None,
            thinking_budget=None,
        )
        await writer.write_provider_response(
            request_id="lock-check",
            inner_turn=1,
            model="glm-test",
            text="",
            thinking="",
            tool_requests=[],
            usage={},
        )
    if verdict:
        await writer.write_event(
            VerifierVerdict(
                request_id="r1",
                verdict_id="v1",
                checkpoint_id="r1:2",
                status="on_track",
                severity="none",
                confidence=0.75,
                rationale="touching DONE.txt is still the task",
                triggering_signals=("constraint_touch",),
                judge_tokens=40,
            )
        )
    return root / "session"


def _convert(session: Path, **extra: Any) -> Cassette:
    return from_transcript(session, scenario="demo/read", harness_commit="abc123", **extra)


def test_each_provider_response_becomes_one_call(tmp_path: Path) -> None:
    session = asyncio.run(_write_session(tmp_path))
    raw = [json.loads(line) for line in (session / "transcript.ndjson").read_text().splitlines()]
    assert any("schema_seq" in str(r.get("tools")) for r in raw), "second request stubs the schema"
    assert any(r.get("changed") is True for r in raw), "second snapshot is a diff"

    cassette = _convert(session)

    assert cassette.schema_version == 1
    assert cassette.kind == "trajectory"
    assert (cassette.provider, cassette.model) == ("ollama-cloud", "glm-test")
    assert cassette.harness_commit == "abc123"
    assert cassette.scenario == "demo/read"
    assert [call.index for call in cassette.calls] == [0, 1]
    first, second = (call.model_dump(exclude_none=True) for call in cassette.calls)
    assert [e["type"] for e in first["events"]] == ["thinking", "tool_call", "usage", "done"]
    assert first["events"][0] == {
        "type": "thinking",
        "text": "open the file",
        "signature": "sig-1",
    }
    assert first["events"][1] == {
        "type": "tool_call",
        "call_id": "c1",
        "name": "read_file",
        "args": {"path": "a.py"},
    }
    assert first["events"][2]["input_tokens"] == 100
    assert first["events"][3] == {"type": "done", "truncated": False}
    assert [e["type"] for e in second["events"]] == ["text", "usage", "done"]
    assert second["events"][1]["cache_read_tokens"] == 80
    assert cassette.tool_results == {"c1": '{"ok": true}'}


def test_stubbed_schema_and_message_counts_are_resolved(tmp_path: Path) -> None:
    cassette = _convert(asyncio.run(_write_session(tmp_path)))
    assert [call.request.tool_names for call in cassette.calls] == [["read_file", "glob"]] * 2
    assert [call.request.message_count for call in cassette.calls] == [2, 4]
    assert cassette.tool_schema_digest == tool_schema_digest(_TOOLS)


def test_prompt_digest_comes_from_the_first_full_prompt(tmp_path: Path) -> None:
    cassette = _convert(asyncio.run(_write_session(tmp_path)))
    first = _system_text("/tmp/one")
    assert cassette.prompt_digest == prompt_digest(first)
    # The workspace path sits after the stable prefix, so a different path hashes the same.
    assert prompt_digest(_system_text("/elsewhere")) == cassette.prompt_digest


def test_truncated_batch_marks_the_call(tmp_path: Path) -> None:
    cassette = _convert(asyncio.run(_write_session(tmp_path, truncated=True)))
    done = [e for e in cassette.calls[0].events if e.type == "done"]
    assert [e.truncated for e in done] == [True]
    later = [e for e in cassette.calls[1].events if e.type == "done"]
    assert [e.truncated for e in later] == [False]


def test_excluded_request_ids_are_dropped(tmp_path: Path) -> None:
    session = asyncio.run(_write_session(tmp_path, probe=True))
    assert len(_convert(session).calls) == 3
    assert len(_convert(session, exclude_request_ids=("lock-check",)).calls) == 2


def test_cassette_text_is_scrubbed(tmp_path: Path) -> None:
    secret = "sk-" + "Zq9xW3vB7nM2kL5pR8tY"
    session = asyncio.run(_write_session(tmp_path, secret=secret))
    cassette = _convert(session)
    assert secret not in cassette.model_dump_json()
    assert "[REDACTED]" in cassette.model_dump_json()


def test_a_transcript_without_provider_records_is_refused(tmp_path: Path) -> None:
    async def write() -> Path:
        writer = TranscriptWriter(
            "s2", workspace_root=tmp_path, provider_records=False, session_dir=tmp_path / "bare"
        )
        await writer.ensure_manifest(model="m", provider="p")
        await writer.write_user_message(request_id="r1", content="hi")
        return tmp_path / "bare"

    with pytest.raises(TranscriptConversionError, match="no completed provider call"):
        _convert(asyncio.run(write()))
    with pytest.raises(TranscriptConversionError, match="no transcript"):
        _convert(tmp_path / "missing")


def test_cli_writes_a_cassette_that_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = asyncio.run(_write_session(tmp_path))
    out = tmp_path / "out" / "demo.cassette.json"
    code = main([str(session), "--scenario", "demo/read", "--out", str(out)])
    assert code == 0
    assert "2 calls" in capsys.readouterr().out
    assert len(load_cassette(out).calls) == 2


def test_dump_and_load_round_trip(tmp_path: Path) -> None:
    cassette = _convert(asyncio.run(_write_session(tmp_path)))
    path = tmp_path / "x" / "c.cassette.json"
    dump_cassette(cassette, path)
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert load_cassette(path) == cassette


def _hand_written(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "kind": "trajectory",
        "calls": [
            {
                "index": 0,
                "events": [
                    {
                        "type": "tool_call",
                        "call_id": "c1",
                        "name": "read_file",
                        "args": {"path": "x"},
                    },
                    {"type": "done"},
                ],
            }
        ],
    }
    body.update(changes)
    return body


def test_model_rejects_a_wrong_version_kind_or_unknown_key() -> None:
    assert Cassette.model_validate(_hand_written()).calls[0].index == 0
    for bad in (
        _hand_written(schema_version=2),
        _hand_written(kind="wire"),
        _hand_written(surprise=1),
        _hand_written(calls=[]),
    ):
        with pytest.raises(ValidationError):
            Cassette.model_validate(bad)


def test_load_rejects_calls_out_of_order(tmp_path: Path) -> None:
    body = _hand_written()
    body["calls"][0]["index"] = 3
    path = tmp_path / "bad.cassette.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="index 3"):
        load_cassette(path)


class _Tool:
    def __init__(self, name: str, properties: dict[str, object] | None) -> None:
        self.name = name
        self.input_schema: dict[str, object] = (
            {"type": "object"}
            if properties is None
            else {"type": "object", "properties": properties}
        )


def _call(name: str = "read_file", **args: object) -> CassetteCall:
    return CassetteCall.model_validate(
        {
            "index": 0,
            "events": [{"type": "tool_call", "call_id": "c1", "name": name, "args": args}],
        }
    )


def test_summary_exchange_stays_in_request_order_and_does_not_steal_chat_tools(
    tmp_path: Path,
) -> None:
    """A summary response that finishes during the next chat call keeps its own slot."""
    writer = TranscriptWriter("sess-summary", workspace_root=tmp_path)

    async def write() -> None:
        await writer.write_provider_request(
            request_id="r1",
            inner_turn=1,
            model="glm-test",
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            tools=_TOOLS[:1],
            thinking_budget=None,
        )
        await writer.write_event(ThinkingBlockComplete(request_id="r1", signature="sig-chat"))
        await writer.write_provider_request(
            request_id="r1",
            inner_turn=1,
            model="glm-test",
            messages=[{"role": "user", "content": [{"type": "text", "text": "summarize"}]}],
            tools=[],
            thinking_budget=None,
            exchange_id="sum-1",
        )
        await writer.write_provider_response(
            request_id="r1",
            inner_turn=1,
            model="glm-test",
            text="raw summary",
            thinking="",
            tool_requests=[],
            usage={"input_tokens": 4, "output_tokens": 2},
            exchange_id="sum-1",
        )
        await writer.write_provider_response(
            request_id="r1",
            inner_turn=1,
            model="glm-test",
            text="answer",
            thinking="looked",
            tool_requests=[],
            usage={"input_tokens": 8, "output_tokens": 1},
        )
        await writer.write_provider_request(
            request_id="r1",
            inner_turn=2,
            model="glm-test",
            messages=[{"role": "user", "content": [{"type": "text", "text": "next"}]}],
            message_offset=2,
            tools=None,
            thinking_budget=None,
        )
        await writer.write_provider_response(
            request_id="r1",
            inner_turn=2,
            model="glm-test",
            text="next answer",
            thinking="",
            tool_requests=[{"call_id": "c2", "name": "read_file", "args": {"path": "a"}}],
            usage={"input_tokens": 9, "output_tokens": 1},
        )

    asyncio.run(write())
    cassette = from_transcript(writer.session_dir, scenario="demo/summary", harness_commit="abc")
    assert [call.request.tool_names for call in cassette.calls] == [
        ["read_file"],
        [],
        ["read_file"],
    ]
    dumped = [call.model_dump() for call in cassette.calls]
    assert dumped[0]["events"][0] == {"type": "thinking", "text": "looked", "signature": "sig-chat"}
    assert dumped[0]["events"][1]["text"] == "answer"
    assert dumped[1]["events"][0] == {"type": "text", "text": "raw summary"}
    assert dumped[2]["events"][0]["name"] == "read_file"


def test_stale_tool_names_a_missing_tool_or_a_missing_argument_key() -> None:
    tools = [_Tool("read_file", {"path": {}}), _Tool("free_form", None)]
    assert stale_tool(_call(path="a"), tools) is None
    assert stale_tool(_call("gone", path="a"), tools) == "gone"
    assert stale_tool(_call(file_path="a"), tools) == "read_file"
    assert stale_tool(_call("free_form", anything=1), tools) is None


def test_digest_warnings_report_each_difference_and_nothing_else() -> None:
    first = _system_text("/tmp/one")
    body = _hand_written(
        tool_schema_digest=tool_schema_digest(_TOOLS),
        prompt_digest=prompt_digest(first),
    )
    body["calls"][0]["request"] = {"tool_names": ["read_file", "glob"], "message_count": 2}
    cassette = Cassette.model_validate(body)
    assert digest_warnings(cassette, tools=_TOOLS, system_text=first) == []
    changed = digest_warnings(cassette, tools=_TOOLS[:1], system_text=first.replace("fixed", "x"))
    assert any(line.startswith("tool_schema_digest") for line in changed)
    assert any(line.startswith("prompt_digest") for line in changed)
    assert any("glob" in line for line in changed)
    assert digest_warnings(Cassette.model_validate(_hand_written()), tools=[], system_text="") == []


def test_stable_prefix_stops_before_runtime_paths() -> None:
    prefix = stable_prompt_prefix(_system_text("/tmp/one"))
    assert prefix.startswith("## monkeybot harness (fixed)")
    assert "Runtime paths" not in prefix
    assert stable_prompt_prefix("plain\n## Current date\n2026") == "plain"


def test_scripted_from_cassette_replays_each_call_then_runs_out() -> None:
    body = _hand_written()
    body["calls"][0]["events"] = [
        {"type": "thinking", "text": "t", "signature": "s"},
        {"type": "tool_call", "call_id": "c1", "name": "read_file", "args": {"path": "x"}},
        {"type": "text", "text": "hi"},
        {"type": "usage", "input_tokens": 1, "output_tokens": 2},
        {"type": "done", "truncated": True},
    ]
    body["calls"].append(
        {"index": 1, "events": [{"type": "text", "text": "bye"}, {"type": "done"}]}
    )
    provider = scripted_from_cassette(Cassette.model_validate(body), name="fake")

    async def drain() -> list[list[object]]:
        seen: list[list[object]] = []
        for _ in range(2):
            seen.append([e async for e in provider.stream([], [], model="m")])
        with pytest.raises(ScriptedProviderExhaustedError):
            [e async for e in provider.stream([], [], model="m")]
        return seen

    first, second = asyncio.run(drain())
    assert first == [
        ThinkingDelta(text="t", signature="s"),
        ToolCall(call_id="c1", name="read_file", args={"path": "x"}),
        TextDelta(text="hi"),
        UsageEvent(input_tokens=1, output_tokens=2),
        Done(truncated=True),
    ]
    assert second == [TextDelta(text="bye"), Done()]


def _ledger(path: Path, thread_id: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE goal_ledger (
            entry_id TEXT, thread_id TEXT, seq INTEGER, verbatim TEXT,
            provenance TEXT, channel TEXT, intent TEXT, status TEXT,
            relates_to TEXT, constraints_json TEXT, done_when_json TEXT,
            created_at_ms INTEGER, source_row_id TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO goal_ledger VALUES (
            'e1', ?, 1, 'finish DONE.txt', 'human', 'user', 'new_goal', 'active',
            NULL, ?, ?, 1, NULL
        )
        """,
        (
            thread_id,
            json.dumps(
                [
                    {
                        "kind": "path_glob",
                        "pattern": "DONE.txt",
                        "source_entry_id": "e1",
                        "verbatim": "Create DONE.txt",
                    }
                ]
            ),
            json.dumps(["DONE.txt"]),
        ),
    )
    conn.commit()
    conn.close()


def test_verdict_and_ledger_become_checker_tape(tmp_path: Path) -> None:
    session = asyncio.run(_write_session(tmp_path, verdict=True))
    db = tmp_path / "monkeybot.db"
    _ledger(db, "s1")
    cassette = _convert(session, ledger=db)

    assert len(cassette.calls) == 2
    assert [row.intent for row in cassette.classifier] == ["new_goal"]
    assert cassette.classifier[0].constraints[0].kind == "path_glob"
    assert cassette.classifier[0].constraints[0].pattern == "DONE.txt"
    assert "source_entry_id" not in cassette.classifier[0].constraints[0].model_dump()
    assert cassette.classifier[0].done_when == ["DONE.txt"]
    assert len(cassette.judge) == 1
    body = json.loads(cassette.judge[0].body)
    assert body == {
        "status": "on_track",
        "severity": "none",
        "confidence": 0.75,
        "rationale": "touching DONE.txt is still the task",
    }
    assert cassette.judge[0].output_tokens == 40
    assert cassette.judge[0].input_tokens == 0

    scenario = Scenario.model_validate(
        {
            "id": "regression/t3/trajectories/verifier/replay_drift_tape",
            "case": "T3-VER-01",
            "mode": "replay",
            "driver": "loop",
            "cassette": "replay_drift_tape.cassette.json",
            "hooks": ["verifier"],
            "messages": ["finish DONE.txt"],
            "config": {"verifier": {"enabled": True}, "env": {"KEEP": "1"}},
        }
    )
    scripted = scripted_scenario(scenario, cassette)
    env = scripted.config.env if scripted.config is not None else {}
    assert env["KEEP"] == "1"
    rows = json.loads(env[CLASSIFIER_ROWS_ENV])
    assert rows[0]["intent"] == "new_goal"
    assert rows[0]["constraints"][0]["pattern"] == "DONE.txt"
    completions = json.loads(env[JUDGE_SCRIPT_ENV])["completions"]
    assert json.loads(completions[0]["body"])["status"] == "on_track"


def test_record_flag_and_variable_mean_the_same(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONKEYBOT_RECORD_CASSETTES", raising=False)
    assert not recording_requested()
    pytester.makepyfile(
        """
        from regression.t3.tools.cassette import recording_requested

        def test_flag():
            assert recording_requested()
        """
    )
    result = pytester.runpytest_inprocess("-p", "regression.plugin", "--record")
    result.assert_outcomes(passed=1)
    assert "MONKEYBOT_RECORD_CASSETTES" not in os.environ, "restored after the run"
    monkeypatch.setenv("MONKEYBOT_RECORD_CASSETTES", "1")
    assert recording_requested()
