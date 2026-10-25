"""Turn-end verification gate: unit behaviour and an end-to-end run through the real loop."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.coding.verification_gate import VerificationGate
from monkeybot.core.config.settings import VerificationGateConfig
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.todo_list.store import TodoListStore
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import HookFakeHistory as FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import CapturingProvider


def _payload(event: HookEvent, ctx: Any = None, **kwargs: Any) -> HookPayload:
    return HookPayload(event=event, thread_id="t1", request_id="r1", ctx=ctx or _ctx(), **kwargs)


def _gate(**kwargs: Any) -> tuple[VerificationGate, HookManager]:
    gate = VerificationGate(VerificationGateConfig(enabled=True, **kwargs))
    mgr = HookManager()
    gate.register(mgr)
    return gate, mgr


async def _tool(
    gate: VerificationGate,
    name: str,
    args: dict[str, Any],
    result: str = "{}",
    error: str | None = None,
) -> None:
    await gate.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tool_name=name,
            tool_args=args,
            tool_result=None if error else result,
            tool_error=error,
        )
    )


async def _final(gate: VerificationGate, ctx: Any = None) -> HookPayload:
    payload = _payload(HookEvent.PRE_FINAL_ANSWER, ctx, assistant_text="done")
    await gate.on_pre_final_answer(payload)
    return payload


async def _before(gate: VerificationGate, ctx: Any = None) -> HookPayload:
    payload = _payload(HookEvent.BEFORE_PROVIDER_REQUEST, ctx)
    await gate.on_before_provider(payload)
    return payload


PYTEST_OK = json.dumps({"ok": True, "stdout": "5 passed", "stderr": "", "exit_code": 0})
PYTEST_FAIL = json.dumps({"ok": False, "stdout": "1 failed", "stderr": "", "exit_code": 1})


@pytest.mark.asyncio
async def test_pure_question_is_never_gated() -> None:
    gate, _ = _gate()
    await _tool(gate, "read_file", {"path": "a.py"}, '{"ok": true}')
    assert (await _before(gate)).hold_assistant_text is False
    assert (await _final(gate)).reject_final_answer is None


@pytest.mark.asyncio
async def test_unverified_edits_hold_then_reject_with_a_file_list() -> None:
    gate, _ = _gate()
    await _tool(gate, "replace_in_file", {"path": "calc/ops.py"}, '{"ok": true}')
    await _tool(gate, "write_file", {"path": "calc/new.py"}, '{"ok": true}')
    assert (await _before(gate)).hold_assistant_text is True
    final = await _final(gate)
    assert final.reject_final_answer is not None
    assert final.reject_final_answer.startswith(
        "[Harness] You changed 2 files (calc/ops.py, calc/new.py)"
    )
    assert "Run the project's test/lint command now" in final.reject_final_answer
    assert "could not verify and why" in final.reject_final_answer


@pytest.mark.asyncio
async def test_rejects_only_up_to_max_rejections_then_stops_holding() -> None:
    gate, _ = _gate(max_rejections=1)
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    assert (await _final(gate)).reject_final_answer is not None
    assert (await _before(gate)).hold_assistant_text is False  # nothing left to reject with
    assert (await _final(gate)).reject_final_answer is None

    never, _ = _gate(max_rejections=0)
    await _tool(never, "write_file", {"path": "a.py"}, '{"ok": true}')
    assert (await _before(never)).hold_assistant_text is False
    assert (await _final(never)).reject_final_answer is None


@pytest.mark.asyncio
async def test_passing_verify_command_clears_failing_one_does_not() -> None:
    gate, _ = _gate()
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    await _tool(gate, "run_command", {"argv": ["python", "-m", "pytest", "-q"]}, PYTEST_FAIL)
    assert (await _final(gate)).reject_final_answer is not None

    gate2, _ = _gate()
    await _tool(gate2, "write_file", {"path": "a.py"}, '{"ok": true}')
    await _tool(gate2, "run_command", {"argv": ["python", "-m", "pytest", "-q"]}, PYTEST_OK)
    assert (await _before(gate2)).hold_assistant_text is False
    assert (await _final(gate2)).reject_final_answer is None
    # A new edit after the passing run needs verifying again.
    await _tool(gate2, "replace_in_file", {"path": "a.py"}, '{"ok": true}')
    assert (await _final(gate2)).reject_final_answer is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"argv": ["pytest"]},
        {"command": "npm test"},
        {"argv": ["npm", "test"]},
        {"argv": ["pnpm", "run", "test"]},
        {"argv": ["cargo", "test", "--all"]},
        {"argv": ["go", "test", "./..."]},
        {"argv": ["make", "check"]},
        {"argv": ["uv", "run", "ruff", "check", "."]},
        {"argv": ["mypy", "src"]},
        {"argv": ["tsc", "--noEmit"]},
        {"argv": ["bash", "-c", "cd sub && pytest -x"]},
        {"argv": ["bash", "-c", "pytest -q 2>&1"]},
    ],
)
async def test_default_verify_commands_are_recognised(args: dict[str, Any]) -> None:
    gate, _ = _gate()
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    await _tool(gate, "run_command", args, PYTEST_OK)
    assert (await _final(gate)).reject_final_answer is None, args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "result"),
    [
        ({"argv": ["ls", "-la"]}, PYTEST_OK),  # not a verify command
        ({"argv": ["cat", "ruff.toml"]}, PYTEST_OK),
        ({"argv": ["pytest"]}, "not json"),  # no explicit ok:true -> not evidence
        ({"argv": ["pytest"]}, json.dumps({"ok": "yes"})),
        ({"argv": ["echo", "pytest passed"]}, PYTEST_OK),
        # Exit 0 does not mean the check passed.
        ({"argv": ["bash", "-c", "pytest || true"]}, PYTEST_OK),
        ({"argv": ["bash", "-c", "pytest; echo ok"]}, PYTEST_OK),
        ({"argv": ["bash", "-c", "echo $(pytest)"]}, PYTEST_OK),
        ({"argv": ["timeout", "30", "bash", "-c", "pytest || true"]}, PYTEST_OK),
        ({"argv": ["python", "-c", "print('pytest')"]}, PYTEST_OK),
        ({"argv": ["cd", "pytest"]}, PYTEST_OK),
        ({"argv": ["bash", "-c", "pytest &"]}, PYTEST_OK),
        ({"argv": ["bash", "-c", 'echo "$(pytest)"']}, PYTEST_OK),
        ({"argv": ["bash", "-c", "echo `pytest`"]}, PYTEST_OK),
        # Starting a job is not a finished run.
        (
            {"argv": ["pytest"], "background": True},
            json.dumps({"ok": True, "background": True, "job_id": "j1", "status": "running"}),
        ),
    ],
)
async def test_non_verifying_commands_do_not_clear_unverified_edits(
    args: dict[str, Any], result: str
) -> None:
    gate, _ = _gate()
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    await _tool(gate, "run_command", args, result)
    assert (await _final(gate)).reject_final_answer is not None, (args, result)


@pytest.mark.asyncio
async def test_custom_verify_commands_replace_the_defaults() -> None:
    gate, _ = _gate(verify_commands=(r"\bjust check\b",))
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    await _tool(gate, "run_command", {"argv": ["pytest"]}, PYTEST_OK)
    assert (await _final(gate)).reject_final_answer is not None
    await _tool(gate, "run_command", {"command": "just check"}, PYTEST_OK)
    assert (await _before(gate)).hold_assistant_text is False


@pytest.mark.asyncio
async def test_failed_tool_calls_are_not_edits() -> None:
    gate, _ = _gate()
    await _tool(gate, "replace_in_file", {"path": "a.py"}, error='{"ok": false}')
    assert (await _final(gate)).reject_final_answer is None


@pytest.mark.asyncio
async def test_apply_patch_paths_come_from_the_result() -> None:
    gate, _ = _gate()
    result = json.dumps(
        {
            "ok": True,
            "files": [{"path": "a.py", "action": "update"}, {"path": "b.py", "action": "add"}],
        }
    )
    await _tool(gate, "apply_patch", {"patch_text": "*** Begin Patch ..."}, result)
    final = await _final(gate)
    assert final.reject_final_answer is not None and "a.py, b.py" in final.reject_final_answer


@pytest.mark.asyncio
async def test_open_todos_reject_but_stale_todos_do_not(tmp_path: Path) -> None:
    store = TodoListStore("s", workspace_root=tmp_path, mirror_to_disk=False)
    await store.add("write the parser")
    await store.add("add tests")
    ctx = dataclasses.replace(_ctx(), todo_store=store)

    gate, _ = _gate()
    await _tool(gate, "read_file", {"path": "a.py"}, '{"ok": true}')
    assert (await _final(gate, ctx)).reject_final_answer is None  # todos predate this message

    await _tool(gate, "todo_list", {"action": "list"}, '{"ok": true}')
    final = await _final(gate, ctx)
    assert final.reject_final_answer is not None
    assert "Open todo items: write the parser, add tests." in final.reject_final_answer
    assert "You changed" not in final.reject_final_answer


@pytest.mark.asyncio
async def test_in_progress_todos_count_as_open(tmp_path: Path) -> None:
    store = TodoListStore("s", workspace_root=tmp_path, mirror_to_disk=False)
    await store.add("write the parser")
    await store.start("t1")
    ctx = dataclasses.replace(_ctx(), todo_store=store)
    gate, _ = _gate()
    await _tool(gate, "todo_list", {"action": "start"}, '{"ok": true}')
    final = await _final(gate, ctx)
    assert final.reject_final_answer is not None
    assert "Open todo items: write the parser." in final.reject_final_answer


@pytest.mark.asyncio
async def test_new_user_message_resets_everything() -> None:
    gate, _ = _gate()
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    assert (await _final(gate)).reject_final_answer is not None
    await gate.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message="next task"))
    assert (await _before(gate)).hold_assistant_text is False
    assert (await _final(gate)).reject_final_answer is None
    await _tool(gate, "write_file", {"path": "b.py"}, '{"ok": true}')
    assert (await _final(gate)).reject_final_answer is not None  # rejection budget is per message


@pytest.mark.asyncio
async def test_user_messages_cannot_grow_thread_state_without_bound() -> None:
    from monkeybot.core.coding.verification_gate import _THREAD_STATE_CAP

    gate, _ = _gate()
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    for index in range(_THREAD_STATE_CAP + 10):
        payload = _payload(HookEvent.USER_MESSAGE, user_message="next")
        payload.thread_id = f"other-{index}"
        await gate.on_user_message(payload)
    assert len(gate._by_thread) <= _THREAD_STATE_CAP
    # The oldest thread was evicted, so its unverified edit no longer blocks.
    assert (await _final(gate)).reject_final_answer is None
    latest = _payload(HookEvent.POST_TOOL, tool_name="write_file", tool_args={"path": "b.py"})
    latest.thread_id = f"other-{_THREAD_STATE_CAP + 9}"
    latest.tool_result = '{"ok": true}'
    await gate.on_post_tool(latest)
    final = _payload(HookEvent.PRE_FINAL_ANSWER, assistant_text="done")
    final.thread_id = latest.thread_id
    await gate.on_pre_final_answer(final)
    assert final.reject_final_answer is not None


@pytest.mark.asyncio
async def test_threads_are_independent() -> None:
    gate, _ = _gate()
    await _tool(gate, "write_file", {"path": "a.py"}, '{"ok": true}')
    other = _payload(HookEvent.PRE_FINAL_ANSWER, assistant_text="x")
    other.thread_id = "t2"
    await gate.on_pre_final_answer(other)
    assert other.reject_final_answer is None


@pytest.mark.asyncio
async def test_a_buggy_observation_never_raises() -> None:
    gate, _ = _gate()
    await gate.on_post_tool(_payload(HookEvent.POST_TOOL, tool_name="write_file", tool_args=None))
    await gate.on_post_tool(
        _payload(HookEvent.POST_TOOL, tool_name="run_command", tool_args={"argv": 5})
    )


# --- end to end through the real loop ---


class _ScriptedExecutor(RecordingExecutor):
    """Returns canned JSON per tool name so POST_TOOL sees realistic results."""

    RESULTS = {
        "write_file": json.dumps({"ok": True, "path": "a.py", "bytes": 3}),
        "run_command": PYTEST_OK,
    }

    async def execute(self, *, call: ToolCall, ctx: Any) -> ToolExecutionResult:
        self.calls.append(call)
        return ToolExecutionResult.ok_text(self.RESULTS[call.name])


def _tool_turn(call_id: str, name: str, args: dict[str, Any]) -> list[Any]:
    return [ToolCall(call_id=call_id, name=name, args=args), Done()]


async def _run_loop(
    script: list[list[Any]], gate: bool = True
) -> tuple[list[Any], CapturingProvider, FakeHistory]:
    mgr = HookManager()
    if gate:
        VerificationGate(VerificationGateConfig(enabled=True)).register(mgr)
    prov = CapturingProvider(script)
    hist = FakeHistory()
    ctx = dataclasses.replace(
        _ctx(),
    )
    events = [
        e
        async for e in run(
            "fix it",
            ctx,
            provider=prov,
            history=hist,
            inspectors=[AllowInspector()],
            tool_executor=_ScriptedExecutor(),
            max_turns=10,
            hook_manager=mgr,
        )
    ]
    return events, prov, hist


@pytest.mark.asyncio
async def test_loop_rejects_an_unverified_final_answer_then_accepts_the_verified_one() -> None:
    script = [
        _tool_turn("c1", "write_file", {"path": "a.py", "content": "x"}),
        [TextDelta(text="All done, it works!"), Done()],
        _tool_turn("c2", "run_command", {"argv": ["python", "-m", "pytest", "-q"]}),
        [TextDelta(text="Verified: pytest -q -> 5 passed."), Done()],
    ]
    events, prov, hist = await _run_loop(script)
    deltas = "".join(e.delta for e in events if getattr(e, "kind", None) == "AssistantDelta")
    assert (
        deltas == "Verified: pytest -q -> 5 passed."
    )  # the unverified claim never reached the user
    gates = [
        e
        for e in events
        if getattr(e, "kind", None) == "HarnessIntervention"
        and e.intervention == "final_answer_gate"
    ]
    assert len(gates) == 1 and "a.py" in gates[0].detail
    assert prov._idx == 4
    assert "You changed 1 file (a.py)" in prov.system_texts[2]
    assert events[-1].kind == "TurnComplete"


@pytest.mark.asyncio
async def test_loop_accepts_an_honest_unverified_answer_after_one_rejection() -> None:
    script = [
        _tool_turn("c1", "write_file", {"path": "a.py", "content": "x"}),
        [TextDelta(text="All done!"), Done()],
        [TextDelta(text="Not verified: I could not run the tests here."), Done()],
    ]
    events, prov, _ = await _run_loop(script)
    deltas = "".join(e.delta for e in events if getattr(e, "kind", None) == "AssistantDelta")
    assert deltas == "Not verified: I could not run the tests here."
    assert prov._idx == 3


@pytest.mark.asyncio
async def test_loop_without_the_gate_ends_on_the_first_answer() -> None:
    script = [
        _tool_turn("c1", "write_file", {"path": "a.py", "content": "x"}),
        [TextDelta(text="All done!"), Done()],
    ]
    events, prov, _ = await _run_loop(script, gate=False)
    deltas = "".join(e.delta for e in events if getattr(e, "kind", None) == "AssistantDelta")
    assert deltas == "All done!" and prov._idx == 2
