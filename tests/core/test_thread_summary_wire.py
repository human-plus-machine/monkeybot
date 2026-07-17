from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.thread_summary import messages_to_wire
from monkeybot.core.types.content_blocks import (
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)


def test_messages_to_wire_includes_thinking_before_assistant_text() -> None:
    wire = messages_to_wire(
        [
            Message(role="user", content=[Text(text="hi")]),
            Message(
                role="assistant",
                content=[
                    Thinking(thinking="weigh options"),
                    Text(text="final answer"),
                ],
            ),
        ]
    )
    assert wire == [
        {"role": "user", "text": "hi"},
        {"role": "thinking", "text": "weigh options"},
        {"role": "assistant", "text": "final answer"},
    ]


def test_messages_to_wire_redacted_thinking() -> None:
    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[RedactedThinking(data="opaque"), Text(text="ok")],
            )
        ]
    )
    assert wire[0] == {"role": "thinking", "text": "(redacted thinking)"}
    assert wire[1] == {"role": "assistant", "text": "ok"}


def test_messages_to_wire_includes_tool_rows_with_results() -> None:
    wire = messages_to_wire(
        [
            Message(role="user", content=[Text(text="list files")]),
            Message(
                role="assistant",
                content=[
                    Thinking(thinking="need to list"),
                    Text(text="I'll list the directory."),
                    ToolRequest(
                        id="call_1",
                        name="run_command",
                        args={"command": "ls"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="call_1",
                        tool_name="run_command",
                        result=[Text(text="a.txt\nb.txt")],
                    )
                ],
            ),
            Message(role="assistant", content=[Text(text="Found two files.")]),
        ]
    )
    assert wire == [
        {"role": "user", "text": "list files"},
        {"role": "thinking", "text": "need to list"},
        {"role": "assistant", "text": "I'll list the directory."},
        {
            "role": "tool",
            "text": "Shell  ls",
            "tool": "run_command",
            "call_id": "call_1",
            "args": {"command": "ls"},
            "result": "a.txt\nb.txt",
        },
        {"role": "assistant", "text": "Found two files."},
    ]


def test_messages_to_wire_tool_error() -> None:
    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[
                    ToolRequest(id="c2", name="read_file", args={"path": "x.py"}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c2",
                        tool_name="read_file",
                        result=[Text(text="not found")],
                        is_error=True,
                    )
                ],
            ),
        ]
    )
    assert wire[0]["role"] == "tool"
    assert wire[0]["error"] == "not found"
    assert "result" not in wire[0]
