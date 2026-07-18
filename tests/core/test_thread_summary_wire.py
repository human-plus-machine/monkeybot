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


def test_messages_to_wire_includes_image_rows_from_load_file() -> None:
    from monkeybot.core.types.content_blocks import Image

    wire = messages_to_wire(
        [
            Message(role="user", content=[Text(text="make a cat")]),
            Message(
                role="assistant",
                content=[
                    ToolRequest(
                        id="c_img",
                        name="load_file",
                        args={"path": "./generated-media/images/cat.png"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c_img",
                        tool_name="load_file",
                        result=[
                            Image(
                                mime_type="image/png",
                                data="aW1n",
                                metadata={
                                    "path": "./generated-media/images/cat.png",
                                    "filename": "cat.png",
                                },
                            )
                        ],
                    )
                ],
            ),
            Message(role="assistant", content=[Text(text="Here is your cat.")]),
        ]
    )
    assert wire[0]["role"] == "user"
    assert wire[1]["role"] == "tool"
    assert wire[1]["tool"] == "load_file"
    assert wire[2] == {
        "role": "image",
        "text": "cat.png",
        "mime_type": "image/png",
        "path": "./generated-media/images/cat.png",
        "filename": "cat.png",
    }
    assert "data" not in wire[2]
    assert wire[3] == {"role": "assistant", "text": "Here is your cat."}


def test_messages_to_wire_image_without_path_uses_attachment_layout() -> None:
    from monkeybot.core.types.content_blocks import Image

    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[ToolRequest(id="c1", name="load_file", args={"attachment_id": "att_1"})],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c1",
                        tool_name="load_file",
                        result=[
                            Image(
                                mime_type="image/png",
                                data="aW1n",
                                metadata={"filename": "shot.png", "attachment_id": "att_1"},
                            )
                        ],
                    )
                ],
            ),
        ],
        thread_id="sess-9",
    )
    assert wire[1] == {
        "role": "image",
        "text": "shot.png",
        "mime_type": "image/png",
        "path": ".monkeybot/attachments/sess-9/att_1",
        "filename": "att_1",
    }
    assert "data" not in wire[1]
    assert "attachment_id" not in wire[1]


def test_messages_to_wire_recovers_image_from_freeze_stub_and_path() -> None:
    """After freeze, Image blocks become text stubs — still emit role=image via path."""
    from monkeybot.core.attachments.text import render_tool_media_freeze_text

    path = "./generated-media/images/car.png"
    stub = render_tool_media_freeze_text(
        tool_name="load_file",
        attachment_id="att_abc",
        kind="image",
    )
    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[ToolRequest(id="c_img", name="load_file", args={"path": path})],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c_img",
                        tool_name="load_file",
                        result=[Text(text=stub)],
                    )
                ],
            ),
            Message(role="assistant", content=[Text(text="Here is your car.")]),
        ]
    )
    assert wire[0]["role"] == "tool"
    assert wire[1] == {
        "role": "image",
        "text": path,
        "mime_type": "image/png",
        "path": path,
        "filename": "car.png",
    }
    assert "data" not in wire[1]
    assert "attachment_id" not in wire[1]
    assert wire[2] == {"role": "assistant", "text": "Here is your car."}


def test_messages_to_wire_no_image_row_for_non_image_tool_with_png_path_arg() -> None:
    """write_file with a .png path arg must not fabricate a role=image row."""
    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[
                    ToolRequest(
                        id="c1",
                        name="write_file",
                        args={"path": "notes/summary.png", "content": "not an image"},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c1",
                        tool_name="write_file",
                        result=[Text(text="Wrote 30 bytes to notes/summary.png")],
                    )
                ],
            ),
        ]
    )
    assert not any(row.get("role") == "image" for row in wire)


def test_messages_to_wire_no_image_row_for_run_command_with_png_path_arg() -> None:
    """run_command with a .png path arg must not fabricate a role=image row."""
    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[
                    ToolRequest(
                        id="c1",
                        name="run_command",
                        args={
                            "path": "./generated-media/images/foo.png",
                            "command": "ls -la ./generated-media/images/foo.png",
                        },
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c1",
                        tool_name="run_command",
                        result=[Text(text="-rw-r--r-- 1 user staff 1234 foo.png")],
                    )
                ],
            ),
        ]
    )
    assert not any(row.get("role") == "image" for row in wire)


def test_messages_to_wire_truncates_large_tool_payloads() -> None:
    huge = "x" * 10_000
    wire = messages_to_wire(
        [
            Message(
                role="assistant",
                content=[
                    ToolRequest(
                        id="c3",
                        name="write_file",
                        args={"path": "big.txt", "content": huge},
                    ),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="c3",
                        tool_name="write_file",
                        result=[Text(text=huge)],
                    )
                ],
            ),
        ]
    )
    row = wire[0]
    assert row["role"] == "tool"
    assert len(row["args"]["content"]) == 8001  # 8000 + ellipsis
    assert row["args"]["content"].endswith("…")
    assert len(row["result"]) == 8001
    assert row["result"].endswith("…")
    assert row["args"]["path"] == "big.txt"
