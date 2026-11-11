"""The tool list stays put across idle turns, and changes when history is rewritten."""

from __future__ import annotations

import json

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.mcp.session_visibility import IDLE_INNER_TURNS, MCPSessionVisibility
from monkeybot.core.persistence.thread_summary import CONTEXT_SUMMARY_PREFIX
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.turn_loop import _tools_schema_digest
from monkeybot.core.types.content_blocks import Text, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import ToolsRecordingProvider


class _SharedMCP:
    def __init__(self) -> None:
        self._tools = [ToolDef("browser__goto", "Go", {"type": "object"})]

    def known_server_names(self) -> list[str]:
        return ["browser"]

    def catalog_names(self) -> list[str]:
        return ["browser"]

    def is_connected(self, name: str) -> bool:
        return name == "browser"

    def all_tools(self) -> list[ToolDef]:
        return list(self._tools)

    async def wait_until_ready(self) -> None:
        return None


def _is_summary_request(messages: list[Message]) -> bool:
    for message in messages:
        if message.role != "system":
            continue
        for block in message.content:
            if isinstance(block, Text) and block.text.startswith("Output exactly the Markdown"):
                return True
    return False


class _Exec(RecordingExecutor):
    def __init__(self, mcp: _SharedMCP) -> None:
        super().__init__(("ok", None))
        self.mcp = mcp


def _ctx(visibility: MCPSessionVisibility, *, window: int) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("list_dir", "List", {}),
            ToolDef("browser__goto", "Go", {"type": "object"}),
        ],
        user_id=None,
        parent_run_id=None,
        model="fake",
        mcp_visibility=visibility,
        catalog_mcp_servers=("browser",),
        context_window_tokens=window,
    )


class _SummaryAwareProvider(ToolsRecordingProvider):
    """Answers a compaction summary without consuming the chat script."""

    async def stream(
        self, messages, tools, *, model, thinking_budget=None, vertex_google_search=False
    ):  # type: ignore[no-untyped-def]
        if _is_summary_request(list(messages)):
            self.stream_tools.append([tool.name for tool in tools])
            self.stream_messages.append(list(messages))
            yield TextDelta(text="compacted")
            yield Done()
            return
        async for event in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        ):
            yield event


def _message_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, Text):
                parts.append(block.text)
            elif isinstance(block, ToolResponse):
                parts.extend(item.text for item in block.result if isinstance(item, Text))
    return "\n".join(parts)


def test_schema_hash_follows_key_order() -> None:
    first = ToolDef("m", "m", {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}})
    second = ToolDef("m", "m", {"properties": {"b": {"type": "string"}, "a": {"type": "string"}}})
    assert _tools_schema_digest([first])[0] != _tools_schema_digest([second])[0]
    assert json.dumps(first.to_model_schema()) != json.dumps(second.to_model_schema())
    assert _tools_schema_digest([first])[0] == _tools_schema_digest([first])[0]


async def test_idle_turns_send_the_same_tool_payload() -> None:
    visibility = MCPSessionVisibility()
    steps: list[list[object]] = [
        [ToolCall(name="list_dir", args={"path": ".", "nonce": index}, call_id=f"c{index}"), Done()]
        for index in range(12)
    ]
    steps.append([TextDelta(text="done"), Done()])
    provider = ToolsRecordingProvider(steps)
    async for _event in run(
        "list the directory",
        _ctx(visibility, window=200_000),
        provider=provider,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=_Exec(_SharedMCP()),
        max_turns=20,
    ):
        pass
    assert len(provider.stream_tools) == 13
    assert all(names == provider.stream_tools[0] for names in provider.stream_tools)
    assert "browser__goto" in provider.stream_tools[0]
    assert not visibility.is_hidden("browser")
    assert visibility.idle_count("browser") >= IDLE_INNER_TURNS


async def test_compaction_hides_a_due_server_on_the_summary_request() -> None:
    visibility = MCPSessionVisibility()
    for _ in range(IDLE_INNER_TURNS):
        visibility.note_inner_turn({"browser"}, set())
    assert visibility.servers_due() == ["browser"]
    history = FakeHistory(
        [
            Message.text("user" if index % 2 == 0 else "assistant", f"row-{index}-" + ("Q" * 20000))
            for index in range(8)
        ]
    )
    provider = _SummaryAwareProvider(
        [
            [ToolCall(name="list_dir", args={"path": "."}, call_id="c1"), Done()],
            [TextDelta(text="done"), Done()],
        ]
    )
    async for _event in run(
        "continue",
        _ctx(visibility, window=20_000),
        provider=provider,
        history=history,
        inspectors=[AllowInspector()],
        tool_executor=_Exec(_SharedMCP()),
        max_turns=6,
    ):
        pass
    chat = [
        (tools, messages)
        for tools, messages in zip(provider.stream_tools, provider.stream_messages, strict=True)
        if not _is_summary_request(messages) and CONTEXT_SUMMARY_PREFIX in _message_text(messages)
    ]
    assert chat, "no chat request carried the compaction summary"
    assert "browser__goto" not in chat[0][0]
    assert "list_dir" in chat[0][0]
    assert visibility.is_hidden("browser")
    later = _message_text(chat[-1][1])
    assert "because it went unused" in later
    assert "enable_mcp" in later
