"""Spill-file inventory notes for large tool outputs."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from monkeybot.core.context.tool_shapers import (
    classify_content,
    shape_json,
    shape_json_value,
    shape_logs,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.context_budget import diff_inventory_lines

logger = logging.getLogger(__name__)

_INVENTORY_PREFIX = "[Spill inventory —"
_SPILL_DIR_REL = Path(".monkeybot") / "spill"

# Bounded deterministic preview — no LLM. Keep the in-history note small.
_PREVIEW_MAX_CHARS = 2000
_PREVIEW_HEAD_LINES = 40
_PREVIEW_TAIL_LINES = 20
_PREVIEW_JSON_ARRAY_ITEMS = 15


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _try_parse_json(text: str) -> Any | None:
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _unwrap_tool_envelope(text: str) -> tuple[str, bool, Any | None]:
    """Peel common ``{ok, stdout, stderr}`` wrappers for preview only.

    Returns ``(body, unwrapped, parsed_body_or_none)``. The spill file still
    stores the original ``text`` verbatim.
    """
    parsed = _try_parse_json(text)
    if not isinstance(parsed, dict):
        return text, False, parsed
    if "stdout" not in parsed and "stderr" not in parsed:
        return text, False, parsed

    parts: list[str] = []
    stdout = parsed.get("stdout")
    stderr = parsed.get("stderr")
    if stdout not in (None, ""):
        parts.append(str(stdout))
    if stderr not in (None, ""):
        parts.append(str(stderr))
    if not parts:
        return text, False, parsed

    body = "\n".join(parts)
    return body, True, _try_parse_json(body)


def _spill_content_kind(
    body: str,
    tool_name: str,
    *,
    parsed: Any | None,
) -> tuple[str, list[str] | None]:
    """Classify preview body. Returns ``(kind, diff_paths_or_none)``."""
    if parsed is not None:
        return "json", None
    diff_paths = diff_inventory_lines(body)
    if diff_paths:
        return "diff", diff_paths
    return classify_content(body, tool_name=tool_name), None


def _truncate_preview(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n… (+{omitted} chars omitted from spill preview)"


def _head_tail_preview(text: str) -> str:
    lines = text.splitlines()
    head, tail = _PREVIEW_HEAD_LINES, _PREVIEW_TAIL_LINES
    if len(lines) <= head + tail:
        return text
    omitted = len(lines) - head - tail
    return "\n".join(
        [
            *lines[:head],
            f"… (+{omitted} lines omitted from spill preview)",
            *lines[-tail:],
        ]
    )


def _preview_for_kind(
    kind: str,
    body: str,
    *,
    parsed: Any | None,
    diff_paths: list[str] | None,
) -> str:
    if kind == "json":
        if parsed is not None:
            shaped = shape_json_value(parsed, max_array_items=_PREVIEW_JSON_ARRAY_ITEMS)
            return json.dumps(shaped, indent=2, ensure_ascii=False, default=str)
        return shape_json(body, max_array_items=_PREVIEW_JSON_ARRAY_ITEMS)
    if kind == "diff":
        paths = diff_paths or []
        header = f"Changed files ({len(paths)}): " + ", ".join(paths[:80])
        if len(paths) > 80:
            header += f", ... and {len(paths) - 80} more"
        return header + "\n" + _head_tail_preview(body)
    if kind == "logs":
        # Empty keep_patterns → shape_logs uses its default error patterns.
        return shape_logs(
            body,
            max_lines=_PREVIEW_HEAD_LINES + _PREVIEW_TAIL_LINES,
            keep_patterns=(),
            collapse_repeated=True,
        )
    if kind == "code":
        return _head_tail_preview(body)
    return body


def _build_spill_preview(
    text: str,
    *,
    tool_name: str = "unknown",
) -> tuple[str, str, bool, int]:
    """Return ``(kind, preview_text, unwrapped, body_lines)``."""
    body, unwrapped, parsed = _unwrap_tool_envelope(text)
    kind, diff_paths = _spill_content_kind(body, tool_name, parsed=parsed)
    preview = _preview_for_kind(kind, body, parsed=parsed, diff_paths=diff_paths)
    return (
        kind,
        _truncate_preview(preview, _PREVIEW_MAX_CHARS),
        unwrapped,
        _line_count(body),
    )


def _spill_inventory_note_with_kind(
    text: str,
    rel_spill_path: str,
    *,
    tool_name: str = "unknown",
) -> tuple[str, str]:
    """Return ``(inventory_note, kind)``."""
    total_chars = len(text)
    total_lines = _line_count(text)
    kind, preview, unwrapped, body_lines = _build_spill_preview(text, tool_name=tool_name)

    unwrapped_bit = (
        f" | unwrapped_lines={body_lines}"
        if unwrapped and body_lines != total_lines
        else ""
    )
    header = (
        f"{_INVENTORY_PREFIX} {total_chars} total chars, {total_lines} total lines"
        f" | kind={kind} | tool={tool_name}{unwrapped_bit}."
    )
    note = "\n".join(
        [
            header,
            "Preview:",
            preview,
            (
                f"Full output at: {rel_spill_path} — "
                "use read_file with offset/limit only if you need more than the preview.]"
            ),
        ]
    )
    return note, kind


def spill_inventory_note(
    text: str,
    rel_spill_path: str,
    *,
    tool_name: str = "unknown",
) -> str:
    """Build an inventory note with a deterministic content preview (no LLM)."""
    note, _kind = _spill_inventory_note_with_kind(
        text, rel_spill_path, tool_name=tool_name
    )
    return note


def _safe_spill_filename(call_id: str) -> str:
    safe = "".join(c for c in call_id if c.isalnum() or c in "-_")[:200]
    return safe or "call"


def write_spill_with_inventory(
    text: str,
    workspace_root: Path,
    thread_id: str,
    call_id: str,
    *,
    tool_name: str = "unknown",
) -> str:
    """Write raw ``text`` to spill file; return inventory + deterministic preview."""
    rel = f"{_SPILL_DIR_REL.as_posix()}/{thread_id}/{_safe_spill_filename(call_id)}.txt"
    out_path = (Path(workspace_root) / rel).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    note, kind = _spill_inventory_note_with_kind(text, rel, tool_name=tool_name)
    logger.debug(
        "tool result spilled %s",
        kv(tool=tool_name, path=rel, kind=kind, chars=len(text), note_chars=len(note)),
    )
    return note


def spill_root(workspace_root: Path) -> Path:
    """Return ``.monkeybot/spill`` under ``workspace_root``."""
    return Path(workspace_root).resolve() / _SPILL_DIR_REL


def session_spill_dirs(workspace_root: Path, session_id: str) -> list[Path]:
    """Spill directories owned by a chat session (parent + nested subagent threads).

    Parent turns write ``.monkeybot/spill/{session_id}/``. Subagent workers write
    ``.monkeybot/spill/subagent:{session_id}:{suffix}/`` so session-end cleanup
    can remove both with one namespace.
    """
    root = spill_root(workspace_root)
    dirs: list[Path] = [root / session_id]
    if root.is_dir():
        dirs.extend(sorted(root.glob(f"subagent:{session_id}:*")))
    return dirs


def _rmtree_spill(path: Path) -> None:
    """Remove a spill directory; log and re-raise on failure so callers can observe it."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("failed to remove spill directory path=%s", path, exc_info=True)
        raise


async def cleanup_session_spill_files(workspace_root: Path, session_id: str) -> None:
    """Remove session and subagent spill dirs concurrently (off the event loop).

    Spills must survive across user turns within a session so the model can
    ``read_file`` inventory pointers on later turns. Call this on session end
    (gateway DELETE / process teardown), not at turn start.
    """
    targets = [p for p in session_spill_dirs(workspace_root, session_id) if p.exists()]
    if not targets:
        return
    await asyncio.gather(*(asyncio.to_thread(_rmtree_spill, p) for p in targets))


def spill_min_chars_from_env() -> int:
    import os

    raw = os.environ.get("MONKEYBOT_SPILL_MIN_CHARS", "8000").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 8000
