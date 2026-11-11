"""A repeated read_file is stored once, and only while the first copy is still there."""

from __future__ import annotations

import json
from collections.abc import Sequence

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Message
from monkeybot.core.llm.usage import Usage
from monkeybot.core.runtime.context_budget import ContextBudgeter
from monkeybot.core.runtime.history_compaction import _append_budgeted_tool_responses
from monkeybot.core.runtime.read_dedupe import STUB_NOTE
from monkeybot.core.tools.read_ledger import FileReadLedger
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse


class _History:
    def __init__(self) -> None:
        self.rows: dict[str, list[Message]] = {}
        self.loads = 0

    async def load(self, thread_id: str) -> list[Message]:
        self.loads += 1
        return list(self.rows.get(thread_id, []))

    async def append(self, thread_id: str, message: Message) -> None:
        self.rows.setdefault(thread_id, []).append(message)


class _Provider:
    name = "fake"
    supports_streaming = True

    async def stream(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("repeat-read storage must not compact")
        yield  # pragma: no cover

    async def count_input_tokens(self, *_args: object, **_kwargs: object) -> int:
        return 0


def _ctx(ledger: FileReadLedger | None, thread_id: str = "t") -> TurnContext:
    return TurnContext(
        thread_id=thread_id,
        request_id="req",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="fake",
        read_ledger=ledger,
    )


def _body(
    *,
    path: str = "src/a.py",
    token: str = "C13-REPEAT-TOKEN",
    start: int = 1,
    end: int = 31,
    total: int = 31,
    truncated: bool = True,
    next_offset: int | None = 32,
    content: str | None = None,
) -> str:
    if content is None:
        content = token + "\n" + ("x" * 80 + "\n") * 30
    payload: dict[str, object] = {
        "ok": True,
        "path": path,
        "content": content,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "truncated": truncated,
    }
    if next_offset is not None:
        payload["next_offset"] = next_offset
    return json.dumps(payload, ensure_ascii=False)


def _read(call_id: str, text: str, *, is_error: bool = False) -> ToolResponse:
    return ToolResponse(
        id=call_id,
        tool_name="read_file",
        result=[Text(text=text)],
        is_error=is_error,
    )


def _texts(history: _History, thread_id: str = "t") -> list[str]:
    found: list[str] = []
    for message in history.rows.get(thread_id, []):
        for block in message.content:
            if isinstance(block, ToolResponse) and block.tool_name == "read_file":
                found.append("".join(part.text for part in block.result if isinstance(part, Text)))
    return found


async def _store(
    history: _History,
    ctx: TurnContext,
    blocks: Sequence[ContentBlock],
) -> None:
    budgeter = ContextBudgeter(window_tokens=200_000, used_tokens=0)
    async for _event in _append_budgeted_tool_responses(
        chunk_responses=list(blocks),
        ctx=ctx,
        history=history,  # type: ignore[arg-type]
        usage=Usage(),
        budgeter=budgeter,
        provider=_Provider(),  # type: ignore[arg-type]
    ):
        pass


def _ready(thread_id: str = "t") -> tuple[_History, TurnContext, FileReadLedger, str]:
    ledger = FileReadLedger()
    body = _body()
    assert len(body) >= 2000
    return _History(), _ctx(ledger, thread_id), ledger, body


async def test_identical_reread_stores_a_stub_and_does_not_load_history_first() -> None:
    history, ctx, _ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body)])
    assert history.loads == 0
    await _store(history, ctx, [_read("r2", body)])
    assert history.loads == 1
    stored = _texts(history)
    assert stored[0] == body
    stub = json.loads(stored[1])
    assert stub["unchanged"] is True
    assert stub["path"] == "src/a.py"
    assert stub["start_line"] == 1
    assert stub["end_line"] == 31
    assert stub["total_lines"] == 31
    assert stub["truncated"] is True
    assert stub["next_offset"] == 32
    assert stub["note"] == STUB_NOTE
    assert "content" not in stub
    assert body not in stored[1]


async def test_read_after_a_stub_stores_the_full_text_then_stubs_again() -> None:
    history, ctx, _ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body)])
    await _store(history, ctx, [_read("r2", body)])
    await _store(history, ctx, [_read("r3", body)])
    await _store(history, ctx, [_read("r4", body)])
    stored = _texts(history)
    assert stored[2] == body
    assert json.loads(stored[3])["unchanged"] is True


async def test_changed_file_stores_the_full_text() -> None:
    history, ctx, _ledger, body = _ready()
    changed = _body(token="C13-CHANGED-TOKEN")
    await _store(history, ctx, [_read("r1", body)])
    await _store(history, ctx, [_read("r2", changed)])
    assert _texts(history)[1] == changed


async def test_different_range_stores_the_full_text() -> None:
    history, ctx, _ledger, body = _ready()
    other = _body(end=10, total=31)
    await _store(history, ctx, [_read("r1", body)])
    await _store(history, ctx, [_read("r2", other)])
    assert _texts(history)[1] == other


async def test_trimmed_original_stores_the_full_text() -> None:
    history, ctx, ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body)])
    history.rows["t"][0] = Message(role="user", content=[_read("r1", body[:80])])
    await _store(history, ctx, [_read("r2", body)])
    assert _texts(history)[1] == body
    shown = ledger.shown_read("t", "src/a.py", 1, 31)
    assert shown is not None and shown.call_id == "r2"


async def test_cleared_ledger_stores_the_full_text() -> None:
    history, ctx, ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body)])
    ledger.clear()
    assert ledger.shown_read("t", "src/a.py", 1, 31) is None
    await _store(history, ctx, [_read("r2", body)])
    assert _texts(history)[1] == body


async def test_removed_row_without_clear_stores_the_full_text() -> None:
    history, ctx, _ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body)])
    history.rows["t"].clear()
    await _store(history, ctx, [_read("r2", body)])
    assert _texts(history)[0] == body


async def test_other_thread_on_the_same_ledger_stores_the_full_text() -> None:
    history, ctx, _ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body)])
    history.rows["other"] = list(history.rows["t"])
    await _store(history, _ctx(ctx.read_ledger, "other"), [_read("r2", body)])
    assert _texts(history, "other")[-1] == body


async def test_short_reread_stores_the_full_text() -> None:
    history, ctx, _ledger, _body_text = _ready()
    short = _body(content="hi", end=1, total=1, truncated=False, next_offset=None)
    assert len(short) < 2000
    await _store(history, ctx, [_read("r1", short)])
    await _store(history, ctx, [_read("r2", short)])
    assert _texts(history) == [short, short]


async def test_no_ledger_stores_the_full_text() -> None:
    history = _History()
    ctx = _ctx(None)
    body = _body()
    await _store(history, ctx, [_read("r1", body)])
    await _store(history, ctx, [_read("r2", body)])
    assert history.loads == 0
    assert _texts(history) == [body, body]


async def test_error_result_is_not_a_stored_read() -> None:
    history, ctx, ledger, body = _ready()
    await _store(history, ctx, [_read("r1", body, is_error=True)])
    assert ledger.shown_read("t", "src/a.py", 1, 31) is None
    await _store(history, ctx, [_read("r2", body)])
    assert _texts(history)[1] == body
