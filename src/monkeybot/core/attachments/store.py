"""Filesystem-backed session attachment store."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from monkeybot.core.attachments.config import (
    ALLOWED_MIME_TYPES,
    IMAGE_MIME_TYPES,
    attachment_ttl_hours,
    max_attachments_per_session,
    max_image_bytes,
    max_pdf_bytes,
)


class AttachmentStoreError(Exception):
    """Base error for attachment store operations."""


class AttachmentTooLargeError(AttachmentStoreError):
    pass


class UnsupportedAttachmentTypeError(AttachmentStoreError):
    pass


class AttachmentSessionLimitError(AttachmentStoreError):
    pass


@dataclass(frozen=True)
class StoredAttachment:
    attachment_id: str
    mime_type: str
    size_bytes: int
    filename: str
    created_at_ms: int


def sniff_mime(header: bytes) -> str | None:
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:5] == b"%PDF-":
        return "application/pdf"
    return None


def new_attachment_id() -> str:
    return f"att_{uuid.uuid4().hex}"


class AttachmentStore(Protocol):
    def exists(self, session_id: str, attachment_id: str) -> bool: ...

    def count_session(self, session_id: str) -> int: ...

    def save(
        self,
        session_id: str,
        *,
        data: bytes,
        mime_type: str,
        filename: str,
    ) -> StoredAttachment: ...

    def read(self, session_id: str, attachment_id: str) -> tuple[bytes, str, str]: ...

    def read_base64(self, session_id: str, attachment_id: str) -> tuple[str, str, str]: ...


class FilesystemAttachmentStore:
    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root.resolve()

    def _session_dir(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace("..", "_")
        return self._root / ".monkeybot" / "attachments" / safe

    def _path_for(self, session_id: str, attachment_id: str) -> Path:
        safe_id = attachment_id.replace("/", "_").replace("..", "_")
        return self._session_dir(session_id) / safe_id

    def exists(self, session_id: str, attachment_id: str) -> bool:
        return self._path_for(session_id, attachment_id).is_file()

    def count_session(self, session_id: str) -> int:
        d = self._session_dir(session_id)
        if not d.is_dir():
            return 0
        return sum(1 for p in d.iterdir() if p.is_file() and not p.name.endswith(".json"))

    def save(
        self,
        session_id: str,
        *,
        data: bytes,
        mime_type: str,
        filename: str,
    ) -> StoredAttachment:
        if mime_type not in ALLOWED_MIME_TYPES:
            raise UnsupportedAttachmentTypeError(f"unsupported mime type: {mime_type}")
        max_bytes = max_image_bytes() if mime_type in IMAGE_MIME_TYPES else max_pdf_bytes()
        if len(data) > max_bytes:
            raise AttachmentTooLargeError(f"attachment exceeds {max_bytes} bytes")
        sniffed = sniff_mime(data[:512])
        if sniffed is not None and sniffed != mime_type:
            raise UnsupportedAttachmentTypeError(
                f"declared mime {mime_type!r} does not match content ({sniffed})"
            )
        if self.count_session(session_id) >= max_attachments_per_session():
            raise AttachmentSessionLimitError("session attachment limit reached")

        attachment_id = new_attachment_id()
        path = self._path_for(session_id, attachment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        created_at_ms = int(time.time() * 1000)
        meta_path = path.with_suffix(path.suffix + ".json")
        meta_path.write_text(
            json.dumps(
                {
                    "attachment_id": attachment_id,
                    "mime_type": mime_type,
                    "filename": filename,
                    "size_bytes": len(data),
                    "created_at_ms": created_at_ms,
                }
            ),
            encoding="utf-8",
        )
        return StoredAttachment(
            attachment_id=attachment_id,
            mime_type=mime_type,
            size_bytes=len(data),
            filename=filename,
            created_at_ms=created_at_ms,
        )

    def read(self, session_id: str, attachment_id: str) -> tuple[bytes, str, str]:
        path = self._path_for(session_id, attachment_id)
        if not path.is_file():
            raise FileNotFoundError(f"attachment not found: {attachment_id}")
        self._check_ttl(path)
        meta = self._read_meta(path)
        mime = str(meta.get("mime_type", "application/octet-stream"))
        filename = str(meta.get("filename", attachment_id))
        return path.read_bytes(), mime, filename

    def read_base64(self, session_id: str, attachment_id: str) -> tuple[str, str, str]:
        data, mime, filename = self.read(session_id, attachment_id)
        return base64.b64encode(data).decode("ascii"), mime, filename

    def _read_meta(self, path: Path) -> dict[str, object]:
        meta_path = path.with_suffix(path.suffix + ".json")
        if meta_path.is_file():
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        return {}

    def _check_ttl(self, path: Path) -> None:
        meta = self._read_meta(path)
        created = meta.get("created_at_ms")
        if isinstance(created, (int, float)):
            age_ms = int(time.time() * 1000) - int(created)
            if age_ms > attachment_ttl_hours() * 3600 * 1000:
                raise FileNotFoundError("attachment expired")
