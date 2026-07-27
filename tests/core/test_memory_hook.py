"""Tests for :mod:`monkeybot.core.memory.hook`."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.memory.hook import MemoryHook
from monkeybot.core.workspace import create_workspace_storage


def _local_st(root: Path):
    return create_workspace_storage("local://" + str(root.resolve()))


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
    )


def _payload(event: HookEvent, **kw: Any) -> HookPayload:
    return HookPayload(event=event, thread_id="t1", request_id="r1", ctx=_ctx(), **kw)


# ----------------------------------------------------------------- write


@pytest.mark.asyncio
async def test_user_message_appends_to_chat_log_not_raw(tmp_path: Path) -> None:
    """Lever 3: user messages go to chat_log.md, never to raw/."""
    mem = tmp_path / "memory"
    hook = MemoryHook(storage=_local_st(mem))
    await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message="Hi, I am Karthik"))

    assert not (mem / "raw").exists(), "user messages must not enter the organizer queue"
    log = mem / "chat_log.md"
    assert log.exists()
    body = log.read_text(encoding="utf-8")
    assert "# Chat Log" in body
    assert "Karthik" in body
    assert body.count("\n- [") == 1


@pytest.mark.asyncio
async def test_user_message_chat_log_appends_one_line_per_message(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    hook = MemoryHook(storage=_local_st(mem))
    for msg in ("first", "second\nwith newline", "third"):
        await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message=msg))

    body = (mem / "chat_log.md").read_text(encoding="utf-8")
    assert body.count("\n- [") == 3, "each message must be exactly one line"
    assert "first" in body and "second with newline" in body and "third" in body


@pytest.mark.asyncio
async def test_user_message_empty_or_whitespace_writes_nothing(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message=None))
    await hook.on_user_message(_payload(HookEvent.USER_MESSAGE, user_message="   \n  "))
    assert not (tmp_path / "memory" / "chat_log.md").exists()
    assert not (tmp_path / "memory" / "raw").exists()


@pytest.mark.asyncio
async def test_post_tool_writes_truncated_result_and_error(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    long_result = "x" * 6000
    await hook.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tool_name="run_command",
            tool_args={"command": "ls"},
            tool_result=long_result,
            tool_error=None,
        )
    )

    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 1
    body = raw_files[0].read_text(encoding="utf-8")
    assert "run_command" in body
    assert '"command": "ls"' in body
    assert "[...truncated]" in body, "long result must be truncated"


@pytest.mark.asyncio
async def test_post_tool_without_tool_name_writes_nothing(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    await hook.on_post_tool(_payload(HookEvent.POST_TOOL, tool_name=None))
    assert not (tmp_path / "memory" / "raw").exists()


@pytest.mark.asyncio
async def test_post_tool_skips_read_only_tools_on_success(tmp_path: Path) -> None:
    """read_file / load_file / search_memory / list_skills successes are not captured."""
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    for name in ("read_file", "load_file", "search_memory", "list_skills"):
        await hook.on_post_tool(
            _payload(
                HookEvent.POST_TOOL,
                tool_name=name,
                tool_args={"path": f"x-{name}.md"},
                tool_result="ok",
                tool_error=None,
            )
        )
    assert not (tmp_path / "memory" / "raw").exists()


@pytest.mark.asyncio
async def test_post_tool_sanitizes_data_uri_before_raw_write(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    # sanitize_tool_result_text only strips data-URIs with >=200 base64 chars.
    b64 = ("A" * 200) + "=="
    await hook.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tool_name="custom_tool",
            tool_args={},
            tool_result=f"preview data:image/png;base64,{b64} done",
            tool_error=None,
        )
    )
    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 1
    body = raw_files[0].read_text(encoding="utf-8")
    assert f"data:image/png;base64,{b64}" not in body
    assert "omitted" in body.lower()


@pytest.mark.asyncio
async def test_post_tool_captures_read_only_tool_errors(tmp_path: Path) -> None:
    """Errors on read-only tools are signal; they MUST be captured."""
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    await hook.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tool_name="read_file",
            tool_args={"path": "missing.md"},
            tool_result=None,
            tool_error="file not found",
        )
    )
    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 1
    body = raw_files[0].read_text(encoding="utf-8")
    assert "file not found" in body


@pytest.mark.asyncio
async def test_post_tool_dedups_identical_calls_within_ttl(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    args = {"command": "echo hi"}
    for _ in range(5):
        await hook.on_post_tool(
            _payload(
                HookEvent.POST_TOOL,
                tool_name="run_command",
                tool_args=args,
                tool_result="hi",
            )
        )
    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 1, "identical calls within TTL must dedup to one"


@pytest.mark.asyncio
async def test_post_tool_different_args_are_not_deduped(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    for cmd in ("ls", "pwd", "date"):
        await hook.on_post_tool(
            _payload(
                HookEvent.POST_TOOL,
                tool_name="run_command",
                tool_args={"command": cmd},
                tool_result="ok",
            )
        )
    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 3


@pytest.mark.asyncio
async def test_post_tool_dedup_expires_after_ttl(tmp_path: Path) -> None:
    """With a near-zero TTL, the second identical call is no longer a duplicate."""
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), dedup_ttl_sec=0.0)
    args = {"command": "echo hi"}
    for _ in range(3):
        await hook.on_post_tool(
            _payload(
                HookEvent.POST_TOOL,
                tool_name="run_command",
                tool_args=args,
                tool_result="hi",
            )
        )
    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 3


@pytest.mark.asyncio
async def test_post_tool_dedup_keys_separate_success_and_error(tmp_path: Path) -> None:
    """Same (tool, args): one success then one error must each produce a file."""
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    args = {"command": "flaky"}
    await hook.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tool_name="run_command",
            tool_args=args,
            tool_result="ok",
            tool_error=None,
        )
    )
    await hook.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tool_name="run_command",
            tool_args=args,
            tool_result=None,
            tool_error="boom",
        )
    )
    raw_files = list((tmp_path / "memory" / "raw").glob("*.md"))
    assert len(raw_files) == 2


@pytest.mark.asyncio
async def test_post_turn_schedules_organizer_once_and_debounces(tmp_path: Path) -> None:
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_organizer() -> None:
        calls["n"] += 1
        started.set()
        await release.wait()

    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=fake_organizer)

    # Three POST_TURN events back-to-back; while the first organizer is running
    # the rest must be no-ops (debounce).
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert calls["n"] == 1

    release.set()
    await asyncio.sleep(0.02)
    assert calls["n"] == 1, "debounce must coalesce concurrent triggers"

    # After the first run completes, a new POST_TURN can schedule another run.
    started.clear()
    release.clear()

    async def fake_organizer_2() -> None:
        calls["n"] += 1
        started.set()
        await release.wait()

    hook._organizer_runner = fake_organizer_2  # type: ignore[assignment]
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert calls["n"] == 2
    release.set()
    await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_post_turn_without_runner_is_noop(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=None)
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))


@pytest.mark.asyncio
async def test_session_end_runs_organizer_synchronously(tmp_path: Path) -> None:
    calls = {"n": 0}

    async def runner() -> None:
        calls["n"] += 1

    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=runner)
    await hook.on_session_end(_payload(HookEvent.SESSION_END))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_organizer_failure_is_swallowed(tmp_path: Path) -> None:
    async def boom() -> None:
        raise RuntimeError("organizer-broken")

    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=boom)
    await hook.on_session_end(_payload(HookEvent.SESSION_END))  # must not raise


# ------------------------------------------------------------------ read


@pytest.mark.asyncio
async def test_pre_turn_injects_memory_lines_for_keyword_hits(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "user_profile.md").write_text(
        "Name: Karthik\nPreferred language: Python\n", encoding="utf-8"
    )

    hook = MemoryHook(storage=_local_st(mem))
    p = _payload(HookEvent.PRE_TURN, user_message="Hi, can you remind me of my name?")
    await hook.on_pre_turn(p)

    assert any("Karthik" in line for line in p.inject_memory_lines)


@pytest.mark.asyncio
async def test_pre_turn_with_no_keywords_does_nothing(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    p = _payload(HookEvent.PRE_TURN, user_message="hi")
    await hook.on_pre_turn(p)
    assert p.inject_memory_lines == []


@pytest.mark.asyncio
async def test_pre_turn_caps_at_max_hits(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    # 10 files all matching the keyword "python"
    for i in range(10):
        (mem / f"f{i}.md").write_text(f"Python fact {i}\n", encoding="utf-8")

    hook = MemoryHook(storage=_local_st(mem), max_retrieval_hits=2)
    p = _payload(HookEvent.PRE_TURN, user_message="Tell me about python preferences")
    await hook.on_pre_turn(p)
    assert len(p.inject_memory_lines) == 2


@pytest.mark.asyncio
async def test_pre_tool_injects_inject_text_for_relevant_tool(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "auth_notes.md").write_text(
        "src/middleware/auth.ts uses jose for Edge runtime.\n", encoding="utf-8"
    )

    hook = MemoryHook(storage=_local_st(mem))
    p = _payload(
        HookEvent.PRE_TOOL,
        tool_name="read_file",
        tool_args={"path": "src/middleware/auth.ts"},
    )
    await hook.on_pre_tool(p)
    assert p.inject_text is not None
    assert "jose" in p.inject_text


@pytest.mark.asyncio
async def test_pre_tool_skips_unknown_tool(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    p = _payload(HookEvent.PRE_TOOL, tool_name="list_skills", tool_args={})
    await hook.on_pre_tool(p)
    assert p.inject_text is None


@pytest.mark.asyncio
async def test_pre_tool_without_relevant_args_does_nothing(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"))
    p = _payload(HookEvent.PRE_TOOL, tool_name="read_file", tool_args={"offset": 1})
    await hook.on_pre_tool(p)
    assert p.inject_text is None


# -------------------------------------------------------- HookManager wire


@pytest.mark.asyncio
async def test_gc_processed_noop_when_directory_missing(tmp_path: Path) -> None:
    storage = _local_st(tmp_path / "memory")
    stats = await storage.gc_prefix("raw/processed/", 7 * 24 * 60 * 60)
    assert stats == {"scanned": 0, "deleted": 0, "errors": 0}


@pytest.mark.asyncio
async def test_gc_processed_deletes_only_old_files(tmp_path: Path) -> None:
    import os as _os

    mem = tmp_path / "memory"
    processed = mem / "raw" / "processed"
    processed.mkdir(parents=True)

    fresh = processed / "fresh.md"
    fresh.write_text("recent", encoding="utf-8")
    stale = processed / "stale.md"
    stale.write_text("old", encoding="utf-8")
    # Backdate stale by 10 days.
    old_mtime = time.time() - (10 * 24 * 60 * 60)
    _os.utime(stale, (old_mtime, old_mtime))

    storage = _local_st(mem)
    stats = await storage.gc_prefix("raw/processed/", 7 * 24 * 60 * 60)

    assert stats["scanned"] == 2
    assert stats["deleted"] == 1
    assert fresh.exists()
    assert not stale.exists()


@pytest.mark.asyncio
async def test_register_attaches_all_events_and_e2e(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mgr = HookManager()
    hook = MemoryHook(storage=_local_st(mem))
    hook.register(mgr)

    await mgr.fire(_payload(HookEvent.USER_MESSAGE, user_message="hello"))
    await mgr.fire(
        _payload(
            HookEvent.POST_TOOL,
            tool_name="run_command",
            tool_args={"command": "ls"},
            tool_result="output",
        )
    )

    # user message lands in chat_log.md, not raw/
    assert (mem / "chat_log.md").exists()
    assert "hello" in (mem / "chat_log.md").read_text(encoding="utf-8")
    # tool call still lands in raw/ for the organizer
    raw = list((mem / "raw").glob("*.md"))
    kinds = [p.name for p in raw]
    assert any("post_tool_run_command" in n for n in kinds)
    assert not any("user_message" in n for n in kinds)


@pytest.mark.asyncio
async def test_flush_noop_when_no_task(tmp_path: Path) -> None:
    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=None)
    await hook.flush()


@pytest.mark.asyncio
async def test_flush_awaits_running_task(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_organizer() -> None:
        started.set()
        await release.wait()

    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=fake_organizer)
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    flushed = asyncio.Event()
    entering_flush = asyncio.Event()

    async def do_flush() -> None:
        entering_flush.set()
        await hook.flush()
        flushed.set()

    t = asyncio.create_task(do_flush())
    await asyncio.wait_for(entering_flush.wait(), timeout=1.0)
    assert not flushed.is_set()
    release.set()
    await asyncio.wait_for(flushed.wait(), timeout=1.0)
    await t


@pytest.mark.asyncio
async def test_flush_noop_when_task_already_done(tmp_path: Path) -> None:
    async def fake_organizer() -> None:
        return

    hook = MemoryHook(storage=_local_st(tmp_path / "memory"), organizer_runner=fake_organizer)
    await hook.on_post_turn(_payload(HookEvent.POST_TURN))
    await hook.flush()
    await hook.flush()
