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

RENDER_IMAGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Workspace-relative path to a PNG/JPEG/GIF/WebP image file.",
        },
        "caption": {
            "type": "string",
            "description": "Optional short caption shown with the image in chat.",
        },
    },
    "required": ["path"],
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


def render_image_tool_def() -> ToolDef:
    return ToolDef(
        "render_image",
        (
            "Display a workspace image inline in the chat UI. "
            "Use after skill scripts write PNG/JPEG/GIF/WebP under ./generated-media/ or similar."
        ),
        RENDER_IMAGE_SCHEMA,
    )
