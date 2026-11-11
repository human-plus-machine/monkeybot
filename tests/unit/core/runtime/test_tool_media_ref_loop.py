"""A load_file image is a reference in history and the same bytes on later calls."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image as PILImage

from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.runtime.events import TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import AttachmentRef, Image, Text, ToolResponse
from testkit.context import _ctx
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import FakeProvider


class _SnapshotHistory(FakeHistory):
    def __init__(self) -> None:
        super().__init__()
        self.appended: list[Message] = []

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        self.appended.append(message)
        await super().append(
            thread_id,
            message,
            turn_id=turn_id,
            message_id=message_id,
        )


class _MediaExecutor:
    def __init__(self, image: Image) -> None:
        self._image = image

    async def execute(self, *, call: ToolCall, ctx: object) -> ToolExecutionResult:
        del ctx
        if call.name == "load_file":
            return ToolExecutionResult.ok_blocks([self._image])
        return ToolExecutionResult.ok_text("ok")

    def running_background_jobs(self) -> list[object]:
        return []


def _png() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (1, 1), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _images(messages: list[Message]) -> list[Image]:
    found: list[Image] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, Image):
                found.append(block)
            elif isinstance(block, ToolResponse):
                found.extend(item for item in block.result if isinstance(item, Image))
    return found


@pytest.mark.asyncio
async def test_load_file_stores_a_ref_and_later_calls_see_the_same_bytes(tmp_path: Path) -> None:
    raw = _png()
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save("t1", data=raw, mime_type="image/png", filename="x.png")
    image = Image(
        mime_type="image/png",
        data=base64.b64encode(raw).decode("ascii"),
        metadata={
            "attachment_id": stored.attachment_id,
            "filename": "x.png",
            "path": "x.png",
        },
    )
    provider = FakeProvider(
        [
            [ToolCall(call_id="c1", name="load_file", args={"path": "x.png"}), Done()],
            [ToolCall(call_id="c2", name="run_command", args={"command": "true"}), Done()],
            [TextDelta(text="done"), Done()],
        ]
    )
    history = _SnapshotHistory()
    events = []
    async for event in run(
        "look at the image",
        _ctx(workspace_root=tmp_path),
        provider=provider,
        history=history,
        inspectors=[AllowInspector()],
        tool_executor=_MediaExecutor(image),  # type: ignore[arg-type]
        attachment_store=store,
        max_turns=6,
    ):
        events.append(event)

    assert isinstance(events[-1], TurnComplete)
    stored_results = [
        item
        for message in history.appended
        for block in message.content
        if isinstance(block, ToolResponse) and block.tool_name == "load_file"
        for item in block.result
    ]
    assert len(stored_results) == 1
    assert isinstance(stored_results[0], AttachmentRef)
    assert stored_results[0].attachment_id == stored.attachment_id
    assert not any(isinstance(item, Image) for item in stored_results)

    second = _images(provider.stream_messages[1])
    third = _images(provider.stream_messages[2])
    assert len(second) == 1
    assert len(third) == 1
    assert second[0].data == third[0].data == image.data
    assert second[0].mime_type == third[0].mime_type == "image/png"

    frozen = [
        item
        for message in history.rows
        if message.role == "user"
        for block in message.content
        if isinstance(block, ToolResponse) and block.tool_name == "load_file"
        for item in block.result
    ]
    assert frozen
    assert all(isinstance(item, Text) for item in frozen)
    assert any("shown earlier" in item.text for item in frozen if isinstance(item, Text))
