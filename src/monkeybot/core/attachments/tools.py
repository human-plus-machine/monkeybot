"""Attachment-related tool definitions."""

from __future__ import annotations

from monkeybot.core.types.types_tools import ToolDef

LOAD_FILE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Workspace-relative path to an image (png/jpeg/gif/webp) or PDF. "
                "Use for screenshots, generated media, or other workspace files."
            ),
        },
        "attachment_id": {
            "type": "string",
            "description": (
                "Session attachment id from ## Session attachments "
                "(user uploads). Prefer this over path when an id is listed."
            ),
        },
    },
    "required": [],
}


def load_file_tool_def() -> ToolDef:
    return ToolDef(
        "load_file",
        (
            "Load an image or PDF into the conversation for analysis "
            "(vision / document context). Pass either a workspace path or a "
            "session attachment_id. Chat may render the resulting media blocks; "
            "this tool is for model context, not UI-only display. "
            "For plain text files use read_file instead."
        ),
        LOAD_FILE_SCHEMA,
        parallel_safe=True,
        read_only=True,
    )
