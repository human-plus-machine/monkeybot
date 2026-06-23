"""In-memory session attachment catalog (rebuilt from history on load)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text

from .text import parse_attachment_descriptor_text


@dataclass(frozen=True)
class AttachmentRecord:
    attachment_id: str
    filename: str
    mime_type: str
    description: str
    storage_path: str
    uploaded_at_ms: int | None = None
    file_missing: bool = False


@dataclass
class SessionAttachmentCatalog:
    """Derived cache; history frozen Text lines are the durable source of truth."""

    session_id: str
    records: dict[str, AttachmentRecord] = field(default_factory=dict)

    def list_records(self) -> list[AttachmentRecord]:
        return list(self.records.values())

    def contains(self, attachment_id: str) -> bool:
        return attachment_id in self.records

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        return self.records.get(attachment_id)

    def upsert(self, record: AttachmentRecord) -> None:
        self.records[record.attachment_id] = record

    def rebuild_from_history(self, messages: Sequence[Message]) -> None:
        self.records.clear()
        for msg in messages:
            for block in msg.content:
                if not isinstance(block, Text):
                    continue
                parsed = parse_attachment_descriptor_text(block.text)
                if parsed is None:
                    continue
                storage_path = (
                    f".monkeybot/attachments/{self.session_id}/{parsed.attachment_id}"
                )
                self.records[parsed.attachment_id] = AttachmentRecord(
                    attachment_id=parsed.attachment_id,
                    filename=parsed.filename,
                    mime_type=parsed.mime_type,
                    description=parsed.description,
                    storage_path=storage_path,
                )
