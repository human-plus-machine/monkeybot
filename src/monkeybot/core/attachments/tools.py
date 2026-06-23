"""Attachment-related tool definitions."""

from __future__ import annotations

from monkeybot.core.types.types_tools import ToolDef

READ_ATTACHMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "attachment_id": {
            "type": "string",
            "description": "Session attachment id from ## Session attachments.",
        },
    },
    "required": ["attachment_id"],
}


def read_attachment_tool_def() -> ToolDef:
    return ToolDef(
        "read_attachment",
        (
            "Load a session attachment by id for visual or PDF analysis. "
            "Ids are listed under ## Session attachments when present."
        ),
        READ_ATTACHMENT_SCHEMA,
    )
