"""Shared helpers for listing persisted chat threads."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from monkeybot.core.attachments.store import attachment_workspace_path
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.tools.tool_hint import (
    tool_collapsed_title,
    truncate_detail,
    truncate_wire_args,
)
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Image,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)

logger = logging.getLogger(__name__)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Matches freeze stubs from ``render_tool_media_freeze_text``.
_FROZEN_TOOL_MEDIA_RE = re.compile(
    r"^\[(?P<tool>\S+)(?:\s+(?P<attachment_id>att_\S+))?\s+result:\s+"
    r"(?P<kind>image|pdf|file)\s+shown earlier;",
    re.IGNORECASE,
)

# Only these tools can return Image blocks (see core_tool_executor._media_result).
# Scoping the freeze-stub/path-arg recovery below to this set prevents unrelated
# tools (write_file, run_command, ...) whose args merely *contain* an
# image-suffixed path from being misrepresented as image rows on the wire.
_IMAGE_CAPABLE_TOOLS = frozenset({"load_file"})

# Optional top-level fields elevated from task tool result JSON onto wire rows.
_TASK_LINKAGE_KEYS: tuple[str, ...] = ("run_id", "child_thread_id", "subagent_type")

# Subagent runs persist their own transcript under f"{SUBAGENT_THREAD_ID_PREFIX}{parent_thread_id}:{suffix}"
# (see core_tool_executor._run_inline_subagent_with_progress and subagent_worker._async_main) —
# the same literal, not centrally imported there since neither module otherwise depends on
# persistence internals. list_threads() implementations filter this prefix out so a subagent
# that happens to finish after its parent's last turn doesn't outrank the parent's own thread
# as "newest" — which would make `monkeybot chat --continue` resume the subagent's transcript,
# under the main-agent prompt and tools, instead of the actual previous conversation.
SUBAGENT_THREAD_ID_PREFIX = "subagent:"


def reserved_thread_id_error(thread_id: str) -> str | None:
    """Return an error message if ``thread_id`` collides with the reserved
    subagent namespace (case-sensitive, matching the internal prefix exactly
    — see ``SUBAGENT_THREAD_ID_PREFIX``), else ``None``.

    Call this at every entry point that accepts a client-supplied session/
    thread id before it's persisted — a session that collides is not
    rejected outright by storage, but silently excluded from
    ``list_threads``/``--continue`` with no indication why, which is worse
    than a clear 400 at creation time.
    """
    if thread_id.startswith(SUBAGENT_THREAD_ID_PREFIX):
        return (
            f"session_id may not start with the reserved prefix "
            f"{SUBAGENT_THREAD_ID_PREFIX!r} (used internally for subagent "
            "transcripts) — a session with this id would be silently "
            "excluded from list_threads/--continue."
        )
    return None


@dataclass(frozen=True)
class ChatThreadSummary:
    """One row in a chat-thread picker."""

    thread_id: str
    last_message_at: int
    message_count: int
    preview: str


def preview_from_content_blob(content_blob: str, *, max_len: int = 120) -> str:
    """Build a one-line preview from stored JSON content blocks."""
    try:
        raw = json.loads(content_blob)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    joined = " ".join(parts).replace("\n", " ").strip()
    if len(joined) <= max_len:
        return joined
    return joined[: max_len - 1].rstrip() + "…"


def text_from_message(message: Message) -> str:
    """Concatenate Text blocks from a stored message."""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, Text) and block.text.strip():
            parts.append(block.text.strip())
    return "\n".join(parts).strip()


def _text_from_blocks(blocks: list[ContentBlock], *, call_id: str) -> str:
    """Concatenate Text blocks; Image rows are emitted separately on the wire.

    Other non-Text result blocks (e.g. File) still have no wire representation
    and are logged so a missing result is visible when debugging history.
    """
    parts: list[str] = []
    dropped = 0
    for block in blocks:
        if isinstance(block, Text):
            if block.text.strip():
                parts.append(block.text.strip())
        elif isinstance(block, Image):
            continue
        else:
            dropped += 1
    if dropped:
        logger.warning(
            "tool result has non-text blocks dropped from chat-history wire %s",
            kv(call_id=call_id, dropped_blocks=dropped),
        )
    return "\n".join(parts).strip()


def _image_row(
    *,
    path: str,
    mime_type: str,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "role": "image",
        "text": label or path,
        "mime_type": mime_type,
        "path": path,
        "filename": PurePosixPath(path).name,
    }


def _mime_for_path(path: str) -> str:
    return _MIME_BY_SUFFIX.get(PurePosixPath(path).suffix.lower(), "image/png")


def _path_from_tool_args(args: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "file"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _frozen_image_hint(blocks: list[ContentBlock]) -> tuple[str | None, str | None]:
    """Return ``(attachment_id, kind)`` from a media freeze stub, if present.

    Relies on the invariant in ``_freeze_tool_responses`` (freeze.py): a frozen
    tool result is always replaced wholesale with a single ``Text`` stub, never
    mixed with other blocks. If that invariant changes, this must be revisited.
    """
    for block in blocks:
        if not isinstance(block, Text) or not block.text.strip():
            continue
        match = _FROZEN_TOOL_MEDIA_RE.match(block.text.strip())
        if match is None:
            continue
        return match.group("attachment_id"), (match.group("kind") or "").lower()
    return None, None


def _resolve_image_path(
    *,
    path: object,
    attachment_id: object,
    thread_id: str | None,
) -> str | None:
    if isinstance(path, str) and path.strip():
        return path.strip()
    if isinstance(attachment_id, str) and attachment_id.strip() and thread_id and thread_id.strip():
        return attachment_workspace_path(thread_id.strip(), attachment_id.strip())
    return None


def _image_rows_for_tool_response(
    req: ToolRequest,
    resp: ToolResponse,
    *,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Emit image wire rows for a tool response, including post-freeze stubs.

    After a turn, ``freeze_attachments_in_history`` replaces Image blocks with a
    short Text stub. Chat-history resume reconstructs from a durable path only
    (never inline base64).
    """
    rows: list[dict[str, Any]] = []
    for block in resp.result:
        if not isinstance(block, Image):
            continue
        meta = block.metadata or {}
        resolved = _resolve_image_path(
            path=meta.get("path"),
            attachment_id=meta.get("attachment_id"),
            thread_id=thread_id,
        )
        if not resolved:
            logger.warning(
                "skipping image wire row without durable path %s",
                kv(
                    has_data=bool(block.data),
                    filename=meta.get("filename")
                    if isinstance(meta.get("filename"), str)
                    else None,
                ),
            )
            continue
        filename = meta.get("filename")
        label = filename.strip() if isinstance(filename, str) and filename.strip() else resolved
        rows.append(_image_row(path=resolved, mime_type=block.mime_type, label=label))
    if rows:
        return rows

    if req.name not in _IMAGE_CAPABLE_TOOLS:
        return []

    # Frozen stub recovery: Image bytes were replaced with Text.
    path = _path_from_tool_args(dict(req.args or {}))
    attachment_id, freeze_kind = _frozen_image_hint(resp.result)
    if path is None and freeze_kind == "image":
        path = _resolve_image_path(path=None, attachment_id=attachment_id, thread_id=thread_id)
    if not path:
        return []
    if freeze_kind != "image" and PurePosixPath(path).suffix.lower() not in _MIME_BY_SUFFIX:
        return []
    return [_image_row(path=path, mime_type=_mime_for_path(path))]


def _tool_responses_by_id(messages: list[Message]) -> dict[str, ToolResponse]:
    out: dict[str, ToolResponse] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolResponse) and block.id:
                out[block.id] = block
    return out


def _task_linkage_from_result(result_text: str) -> dict[str, str]:
    """Parse task tool result JSON and return elevated linkage fields.

    Returns only keys among run_id / child_thread_id / subagent_type whose
    values are non-empty strings. Returns {} on non-JSON, non-object, or
    missing/invalid fields. Never raises.
    """
    try:
        raw = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in _TASK_LINKAGE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def messages_to_wire(
    messages: list[Message],
    *,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Serialize turns for the chat-history API.

    Assistant messages expand into multiple wire rows when they contain
    thinking / tool-request blocks so UIs can restore the same traces that
    were streamed live over SSE:

    - ``role=thinking`` before assistant text
    - ``role=tool`` for each ``ToolRequest`` (with matching ``ToolResponse``)
    - ``role=image`` for Image blocks (or freeze stubs + durable paths)
    - ``role=assistant`` for Text
    - ``role=user`` for user Text (tool-result-only user rows are omitted)

    Tool ``args`` / ``result`` / ``error`` strings are capped so a detail
    response cannot grow without bound (same limit as CLI expand bodies).
    Image rows never include inline base64 ``data``.
    """
    responses = _tool_responses_by_id(messages)
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            text = text_from_message(msg)
            if text:
                out.append({"role": "user", "text": text})
            continue
        if msg.role != "assistant":
            continue

        thinking_parts: list[str] = []
        text_parts: list[str] = []
        tool_requests: list[ToolRequest] = []
        for block in msg.content:
            if isinstance(block, Thinking) and block.thinking.strip():
                thinking_parts.append(block.thinking.strip())
            elif isinstance(block, RedactedThinking):
                thinking_parts.append("(redacted thinking)")
            elif isinstance(block, Text) and block.text.strip():
                text_parts.append(block.text.strip())
            elif isinstance(block, ToolRequest):
                tool_requests.append(block)

        for thinking in thinking_parts:
            out.append({"role": "thinking", "text": thinking})

        text = "\n".join(text_parts).strip()
        if text:
            out.append({"role": "assistant", "text": text})

        for req in tool_requests:
            resp = responses.get(req.id)
            result_text = _text_from_blocks(resp.result, call_id=req.id) if resp is not None else ""
            error: str | None = None
            if resp is not None and resp.is_error:
                error = result_text or "tool error"
                result_text = ""
            row: dict[str, Any] = {
                "role": "tool",
                "text": tool_collapsed_title(req.name, req.name, dict(req.args)),
                "tool": req.name,
                "call_id": req.id,
                "args": truncate_wire_args(dict(req.args)),
            }
            # Elevate linkage from full result before truncating the stored string.
            if req.name == "task" and result_text:
                row.update(_task_linkage_from_result(result_text))
            if result_text:
                row["result"] = truncate_detail(result_text)
            if error:
                row["error"] = truncate_detail(error)
            out.append(row)
            if resp is not None and not resp.is_error:
                out.extend(_image_rows_for_tool_response(req, resp, thread_id=thread_id))
    return out
