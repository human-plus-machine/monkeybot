"""Tests for ``FilesystemAttachmentStore`` path containment."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.attachments.store import FilesystemAttachmentStore


def test_read_rejects_symlink_attachment_leaf(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("leaked", encoding="utf-8")

    session_id = "plain"
    attachment_id = "aid"
    session_dir = workspace / ".monkeybot" / "attachments" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / attachment_id).symlink_to(secret)

    store = FilesystemAttachmentStore(workspace)
    assert store.exists(session_id, attachment_id) is False
    with pytest.raises(FileNotFoundError, match="attachment not found"):
        store.read(session_id, attachment_id)


def test_read_returns_contained_attachment(tmp_path: Path) -> None:
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save(
        "session-1",
        data=b"hello",
        mime_type="image/png",
        filename="dot.png",
    )
    data, mime, filename = store.read("session-1", stored.attachment_id)
    assert data == b"hello"
    assert mime == "image/png"
    assert filename == "dot.png"
