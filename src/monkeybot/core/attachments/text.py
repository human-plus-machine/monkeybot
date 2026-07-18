"""Frozen attachment descriptor text render/parse."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedAttachmentDescriptor:
    attachment_id: str
    filename: str
    mime_type: str
    description: str


# Exact current load_file wording | exact legacy read_attachment wording.
_ATTACHMENT_LINE_RE = re.compile(
    r'^\[attachment\s+(?P<id>\S+)\s+(?P<filename>.+?)\s+\((?P<mime>[^)]+)\):\s+'
    r'(?P<desc>.+?)\.\s+Call (?:'
    r'load_file\(attachment_id="(?P=id)"\) to reload'
    r'|'
    r'read_attachment\("(?P=id)"\) to view pixels again'
    r')\.\]$'
)


def render_attachment_descriptor_text(
    *,
    attachment_id: str,
    filename: str,
    mime_type: str,
    description: str,
) -> str:
    desc = description.strip() or f"{filename} ({mime_type}) attached by user."
    if len(desc) > 500:
        desc = desc[:497].rstrip() + "..."
    return (
        f'[attachment {attachment_id} {filename} ({mime_type}): {desc}. '
        f'Call load_file(attachment_id="{attachment_id}") to reload.]'
    )


def parse_attachment_descriptor_text(text: str) -> ParsedAttachmentDescriptor | None:
    m = _ATTACHMENT_LINE_RE.match(text.strip())
    if m is None:
        return None
    return ParsedAttachmentDescriptor(
        attachment_id=m.group("id"),
        filename=m.group("filename"),
        mime_type=m.group("mime"),
        description=m.group("desc"),
    )


def render_tool_media_freeze_text(
    *,
    tool_name: str,
    attachment_id: str | None,
    kind: str,
    path: str | None = None,
) -> str:
    path_bit = ""
    if isinstance(path, str) and path.strip():
        path_bit = f" path={path.strip()};"
    if attachment_id:
        return (
            f"[{tool_name} {attachment_id} result: {kind} shown earlier;{path_bit} "
            f'call load_file(attachment_id="{attachment_id}") to reload.]'
        )
    return (
        f"[{tool_name} result: {kind} shown earlier;{path_bit} "
        "call tool again to reload.]"
    )


def filename_from_metadata(metadata: dict[str, object] | None, *, fallback: str) -> str:
    if not metadata:
        return fallback
    for key in ("filename", "name"):
        val = metadata.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return fallback
