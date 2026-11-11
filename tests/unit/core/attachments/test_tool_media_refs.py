"""Tool-result media is stored as a reference and resolved on the way out."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest
from PIL import Image as PILImage

from monkeybot.core.attachments.catalog import referenced_attachment_ids
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.attachments.image_preview import (
    make_provider_preview,
    provider_preview_metadata,
)
from monkeybot.core.attachments.refs import tool_media_to_refs
from monkeybot.core.attachments.resolve import (
    AttachmentResolveError,
    MediaMemo,
    resolve_messages_for_provider,
)
from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.context_budget import _CHARS_PER_TOKEN
from monkeybot.core.runtime.history_compaction import (
    _estimate_message_tokens,
    _raw_block_char_count,
)
from monkeybot.core.types.content_blocks import (
    IMAGE_TOKEN_ESTIMATE,
    AttachmentRef,
    File,
    Image,
    Text,
    ToolResponse,
)


class _MemoryStore:
    def __init__(self, files: dict[tuple[str, str], tuple[bytes, str, str]] | None = None) -> None:
        self.files = dict(files or {})
        self.reads = 0

    def exists(self, session_id: str, attachment_id: str) -> bool:
        return (session_id, attachment_id) in self.files

    def read(self, session_id: str, attachment_id: str) -> tuple[bytes, str, str]:
        self.reads += 1
        found = self.files.get((session_id, attachment_id))
        if found is None:
            raise FileNotFoundError(attachment_id)
        return found

    def count_session(self, session_id: str) -> int:
        return sum(1 for session, _attachment in self.files if session == session_id)

    def save(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("save is not used")

    def copy(self, *_args: object, **_kwargs: object) -> bool:
        return False


class _History:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = list(messages)

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        del thread_id, limit
        return list(self.messages)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.messages = list(messages)


def _png(size: int = 8) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (size, size), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _noisy_png(size: int = 400) -> bytes:
    image = PILImage.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _image_response(image: Image) -> ToolResponse:
    return ToolResponse(id="c1", tool_name="load_file", result=[image])


def _stored_image(data: bytes, attachment_id: str = "att") -> Image:
    return Image(
        mime_type="image/png",
        data=base64.b64encode(data).decode("ascii"),
        metadata={"attachment_id": attachment_id, "filename": "x.png", "path": "x.png"},
    )


def test_tool_media_keeps_base64_without_a_store() -> None:
    image = _stored_image(b"png")
    blocks = tool_media_to_refs([_image_response(image)], attachment_store=None, session_id="s")
    assert blocks[0] == _image_response(image)


def test_tool_media_keeps_base64_without_an_attachment_id() -> None:
    image = Image(mime_type="image/png", data="aaaa", metadata={"filename": "x.png"})
    store = _MemoryStore({("s", "att"): (b"png", "image/png", "x.png")})
    blocks = tool_media_to_refs([_image_response(image)], attachment_store=store, session_id="s")
    assert isinstance(blocks[0], ToolResponse)
    assert blocks[0].result == [image]


def test_tool_media_keeps_base64_when_the_id_is_missing_from_the_store() -> None:
    image = _stored_image(b"png", attachment_id="missing")
    store = _MemoryStore({("s", "att"): (b"png", "image/png", "x.png")})
    blocks = tool_media_to_refs([_image_response(image)], attachment_store=store, session_id="s")
    assert isinstance(blocks[0], ToolResponse)
    assert blocks[0].result == [image]
    assert store.reads == 0


def test_file_ref_records_base64_length() -> None:
    payload = "A" * 40
    block = File(
        mime_type="application/pdf",
        data=payload,
        metadata={"attachment_id": "att", "filename": "a.pdf", "path": "a.pdf"},
    )
    store = _MemoryStore({("s", "att"): (b"%PDF", "application/pdf", "a.pdf")})
    blocks = tool_media_to_refs(
        [ToolResponse(id="c1", tool_name="load_file", result=[block])],
        attachment_store=store,
        session_id="s",
    )
    ref = blocks[0].result[0]
    assert isinstance(blocks[0], ToolResponse)
    assert isinstance(ref, AttachmentRef)
    assert ref.mime_type == "application/pdf"
    assert ref.metadata is not None
    assert ref.metadata["base64_chars"] == len(payload)
    assert "data" not in ref.metadata
    assert block.metadata == {"attachment_id": "att", "filename": "a.pdf", "path": "a.pdf"}


def test_nested_ref_resolves_to_the_tool_preview(tmp_path: Path) -> None:
    raw = _png()
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save("s1", data=raw, mime_type="image/png", filename="x.png")
    image = _stored_image(raw, attachment_id=stored.attachment_id)
    blocks = tool_media_to_refs(
        [_image_response(image)],
        attachment_store=store,
        session_id="s1",
    )
    ref = blocks[0].result[0]
    assert isinstance(blocks[0], ToolResponse)
    assert isinstance(ref, AttachmentRef)
    resolved = resolve_messages_for_provider(
        [Message(role="user", content=list(blocks))],
        attachment_store=store,
        session_id="s1",
    )
    got = resolved[0].content[0]
    assert isinstance(got, ToolResponse)
    media = got.result[0]
    assert isinstance(media, Image)
    assert media.data == image.data
    assert media.mime_type == image.mime_type
    assert media.metadata == image.metadata


def test_reencoded_preview_resolves_to_the_same_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_DIM", "64")
    monkeypatch.setenv("ATTACHMENT_PREVIEW_MAX_BYTES", "12000")
    raw = _noisy_png()
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save("s1", data=raw, mime_type="image/png", filename="n.png")
    preview, preview_mime = make_provider_preview(raw, "image/png")
    meta = {"attachment_id": stored.attachment_id, "filename": "n.png", "path": "n.png"}
    image = Image(
        mime_type=preview_mime,
        data=base64.b64encode(preview).decode("ascii"),
        metadata=provider_preview_metadata(
            meta, original_mime="image/png", preview_mime=preview_mime
        ),
    )
    blocks = tool_media_to_refs(
        [_image_response(image)],
        attachment_store=store,
        session_id="s1",
    )
    resolved = resolve_messages_for_provider(
        [Message(role="user", content=list(blocks))],
        attachment_store=store,
        session_id="s1",
    )
    got = resolved[0].content[0]
    assert isinstance(got, ToolResponse)
    media = got.result[0]
    assert isinstance(media, Image)
    assert media.data == image.data
    assert media.mime_type == image.mime_type
    assert media.metadata == image.metadata


def test_expired_tool_ref_becomes_a_stub_and_a_user_ref_still_raises() -> None:
    store = _MemoryStore()
    ref = AttachmentRef(attachment_id="att", mime_type="image/png", metadata={"path": "x.png"})
    resolved = resolve_messages_for_provider(
        [
            Message(
                role="user",
                content=[ToolResponse(id="c1", tool_name="load_file", result=[ref])],
            )
        ],
        attachment_store=store,
        session_id="s",
    )
    stub = resolved[0].content[0]
    assert isinstance(stub, ToolResponse)
    assert isinstance(stub.result[0], Text)
    assert stub.result[0].text == (
        '[load_file result no longer available; call load_file(attachment_id="att") to reload.]'
    )
    with pytest.raises(AttachmentResolveError):
        resolve_messages_for_provider(
            [Message(role="user", content=[ref])],
            attachment_store=store,
            session_id="s",
        )


def test_memo_skips_a_second_read() -> None:
    raw = b"%PDF-1.4 hello"
    store = _MemoryStore({("s", "att"): (raw, "application/pdf", "a.pdf")})
    ref = AttachmentRef(
        attachment_id="att",
        mime_type="application/pdf",
        metadata={"filename": "a.pdf", "attachment_id": "att"},
    )
    message = Message(
        role="user",
        content=[ToolResponse(id="c1", tool_name="load_file", result=[ref])],
    )
    memo: MediaMemo = {}
    first = resolve_messages_for_provider(
        [message],
        attachment_store=store,
        session_id="s",
        media_memo=memo,
    )
    second = resolve_messages_for_provider(
        [message],
        attachment_store=store,
        session_id="s",
        media_memo=memo,
    )
    assert store.reads == 1
    assert isinstance(first[0].content[0], ToolResponse)
    assert isinstance(second[0].content[0], ToolResponse)
    assert first[0].content[0].result[0] == second[0].content[0].result[0]


@pytest.mark.asyncio
async def test_freeze_stub_matches_for_a_ref_and_an_image() -> None:
    meta = {
        "attachment_id": "att_gen1",
        "filename": "out.png",
        "path": "./generated-media/images/out.png",
    }
    image_history = _History(
        [
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="call_1",
                        tool_name="load_file",
                        result=[Image(data="aW1n", mime_type="image/png", metadata=meta)],
                    )
                ],
            )
        ]
    )
    ref_history = _History(
        [
            Message(
                role="user",
                content=[
                    ToolResponse(
                        id="call_1",
                        tool_name="load_file",
                        result=[
                            AttachmentRef(
                                attachment_id="att_gen1",
                                mime_type="image/png",
                                metadata=meta,
                            )
                        ],
                    )
                ],
            )
        ]
    )
    await freeze_attachments_in_history(
        thread_id="t",
        history=image_history,  # type: ignore[arg-type]
        catalog=None,
        last_assistant_text="",
    )
    await freeze_attachments_in_history(
        thread_id="t",
        history=ref_history,  # type: ignore[arg-type]
        catalog=None,
        last_assistant_text="",
    )
    assert image_history.messages == ref_history.messages
    frozen = image_history.messages[0].content[0]
    assert isinstance(frozen, ToolResponse)
    assert isinstance(frozen.result[0], Text)
    assert "shown earlier" in frozen.result[0].text
    assert "att_gen1" in frozen.result[0].text


def test_referenced_ids_include_a_ref_nested_in_a_tool_response() -> None:
    messages = [
        Message(
            role="user",
            content=[
                AttachmentRef(attachment_id="att_top", mime_type="image/png"),
                ToolResponse(
                    id="c1",
                    tool_name="load_file",
                    result=[AttachmentRef(attachment_id="att_nested", mime_type="image/png")],
                ),
            ],
        )
    ]
    assert referenced_attachment_ids(messages) == ["att_top", "att_nested"]


def test_image_ref_counts_as_the_fixed_estimate() -> None:
    ref = AttachmentRef(attachment_id="att", mime_type="image/png", metadata={"filename": "a.png"})
    assert _raw_block_char_count(ref) == IMAGE_TOKEN_ESTIMATE * _CHARS_PER_TOKEN
    assert _estimate_message_tokens(Message(role="user", content=[ref])) == IMAGE_TOKEN_ESTIMATE


def test_file_ref_counts_the_recorded_base64_length() -> None:
    ref = AttachmentRef(
        attachment_id="att",
        mime_type="application/pdf",
        metadata={"base64_chars": 30_000},
    )
    assert _raw_block_char_count(ref) == 30_000 + len("application/pdf")


def test_other_ref_keeps_the_small_count() -> None:
    ref = AttachmentRef(attachment_id="att_x", mime_type="text/plain")
    assert _raw_block_char_count(ref) == len("att_x") + len("text/plain")
    flagged = AttachmentRef(
        attachment_id="a",
        mime_type="application/pdf",
        metadata={"base64_chars": True},
    )
    assert _raw_block_char_count(flagged) == len("a") + len("application/pdf")
