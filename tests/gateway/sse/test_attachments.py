"""Gateway attachment upload and multimodal reply normalization tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.types.content_blocks import AttachmentRef, ContentBlock, Text
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry


class _CaptureLoopPort:
    def __init__(self) -> None:
        self.last_user_content: list[ContentBlock] | None = None

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> None:
        _ = (session_id, request_id)
        self.last_user_content = list(user_content)


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


@pytest.fixture
def loop_port() -> _CaptureLoopPort:
    return _CaptureLoopPort()


@pytest.fixture
def app(registry: SessionRegistry, loop_port: _CaptureLoopPort, tmp_path: Path):
    application = create_app(loop_port=loop_port, registry=registry)
    application.state.attachment_store = FilesystemAttachmentStore(tmp_path)
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _create_session(client: AsyncClient) -> str:
    res = await client.post("/sessions", json={})
    assert res.status_code == 201
    return res.json()["session_id"]


@pytest.mark.asyncio
async def test_post_attachment_returns_201(client: AsyncClient) -> None:
    sid = await _create_session(client)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    res = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("dot.png", png, "image/png")},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["attachment_id"].startswith("att_")
    assert body["mime_type"] == "image/png"
    assert body["filename"] == "dot.png"
    assert body["size_bytes"] == len(png)


@pytest.mark.asyncio
async def test_reply_with_attachment_ref_normalizes_content(
    client: AsyncClient,
    loop_port: _CaptureLoopPort,
) -> None:
    sid = await _create_session(client)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    up = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("dot.png", png, "image/png")},
    )
    att_id = up.json()["attachment_id"]

    res = await client.post(
        f"/sessions/{sid}/reply",
        json={
            "request_id": "req-mm-1",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "attachmentRef",
                    "attachmentId": att_id,
                    "mimeType": "image/png",
                    "metadata": {"filename": "dot.png"},
                },
            ],
        },
    )
    assert res.status_code == 200
    import asyncio

    await asyncio.sleep(0.05)
    assert loop_port.last_user_content is not None
    assert any(isinstance(b, Text) and b.text == "what is this?" for b in loop_port.last_user_content)
    assert any(
        isinstance(b, AttachmentRef) and b.attachment_id == att_id
        for b in loop_port.last_user_content
    )


@pytest.mark.asyncio
async def test_reply_ambiguous_body_400(client: AsyncClient) -> None:
    sid = await _create_session(client)
    res = await client.post(
        f"/sessions/{sid}/reply",
        json={"request_id": "req-bad", "message": "hi", "content": [{"type": "text", "text": "x"}]},
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "AMBIGUOUS_REPLY_BODY"


@pytest.mark.asyncio
async def test_reply_attachment_only_allowed(
    client: AsyncClient,
    loop_port: _CaptureLoopPort,
) -> None:
    sid = await _create_session(client)
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    up = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("dot.png", png, "image/png")},
    )
    att_id = up.json()["attachment_id"]

    res = await client.post(
        f"/sessions/{sid}/reply",
        json={
            "request_id": "req-ref-only",
            "content": [
                {
                    "type": "attachmentRef",
                    "attachmentId": att_id,
                    "mimeType": "image/png",
                    "metadata": {"filename": "dot.png"},
                },
            ],
        },
    )
    assert res.status_code == 200
    import asyncio

    await asyncio.sleep(0.05)
    assert loop_port.last_user_content is not None
    assert not any(isinstance(b, Text) and b.text.strip() for b in loop_port.last_user_content)
    assert any(
        isinstance(b, AttachmentRef) and b.attachment_id == att_id
        for b in loop_port.last_user_content
    )
