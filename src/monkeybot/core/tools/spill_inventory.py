"""Spill-file inventory notes for large tool outputs."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from monkeybot.core.context.tool_shapers import (
    classify_content,
    shape_json,
    shape_json_value,
    shape_logs,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.path_safety import GLOB_METACHARACTERS, sanitize_path_component
from monkeybot.core.runtime.context_budget import diff_inventory_lines

logger = logging.getLogger(__name__)

_INVENTORY_PREFIX = "[Spill inventory —"
_SPILL_DIR_REL = Path(".monkeybot") / "spill"

# Bounded deterministic preview — no LLM. Keep the in-history note small.
_PREVIEW_MAX_CHARS = 2000
_PREVIEW_HEAD_LINES = 40
_PREVIEW_TAIL_LINES = 20
_PREVIEW_JSON_ARRAY_ITEMS = 15

# Window-derived spill / read policy (matches context_budget._CHARS_PER_TOKEN).
CHARS_PER_TOKEN = 4
SPILL_BASE_FRACTION = 0.02
INLINE_MULTIPLIER = 2.5
READ_MULTIPLIER = 5.0
_DEFAULT_INLINE_HARD_FLOOR = 32_768


@dataclass(frozen=True)
class SpillBudgets:
    """Context-window-derived sizing for spill and read_file."""

    spill_threshold: int
    inline_budget: int
    spill_read_budget: int
    inline_hard_cap: int
    summary_max_chars: int


def _clamp(value: float, lo: float, hi: float) -> int:
    return int(max(lo, min(hi, value)))


@lru_cache(maxsize=8)
def spill_budgets_from_window(window_tokens: int) -> SpillBudgets:
    """Derive spill/read budgets from ``model.context_window`` (tokens)."""
    window = max(1, int(window_tokens))
    window_chars = window * CHARS_PER_TOKEN
    base = window_chars * SPILL_BASE_FRACTION

    spill_threshold = _clamp(base, min(8_000, window_chars * 0.05), 64_000)
    inline_budget = _clamp(base * INLINE_MULTIPLIER, min(16_000, window_chars * 0.15), 128_000)
    spill_read_budget = _clamp(base * READ_MULTIPLIER, min(32_000, window_chars * 0.25), 256_000)
    # Tiny / misconfigured windows can clamp threshold and inline to the same
    # int (often 0). Enforce strict ordering so callers never hit AssertionError.
    inline_budget = max(inline_budget, spill_threshold + 1)
    spill_read_budget = max(spill_read_budget, inline_budget + 1)
    inline_hard_cap = max(_DEFAULT_INLINE_HARD_FLOOR, spill_threshold)
    summary_max_chars = _clamp(base, min(4_096, window_chars * 0.025), 16_384)

    if not (spill_threshold < inline_budget < spill_read_budget):
        raise AssertionError(
            "spill budget ordering broken: "
            f"threshold={spill_threshold} inline={inline_budget} read={spill_read_budget} "
            f"window_tokens={window}"
        )
    return SpillBudgets(
        spill_threshold=spill_threshold,
        inline_budget=inline_budget,
        spill_read_budget=spill_read_budget,
        inline_hard_cap=inline_hard_cap,
        summary_max_chars=summary_max_chars,
    )


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


# Array caps tried when inlining oversized JSON, widest first. A raw prefix of a
# JSON payload is unparseable, so prefer shaped-but-valid output.
_JSON_INLINE_ARRAY_CAPS = (200, 80, 40, 15)


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


def _spill_header(
    text: str,
    *,
    tool_name: str,
    kind: str,
    unwrapped: bool,
    body_lines: int,
) -> str:
    total_chars = len(text)
    total_lines = _line_count(text)
    unwrapped_bit = (
        f" | unwrapped_lines={body_lines}"
        if unwrapped and body_lines != total_lines
        else ""
    )
    return (
        f"{_INVENTORY_PREFIX} {total_chars} total chars, {total_lines} total lines"
        f" | kind={kind} | tool={tool_name}{unwrapped_bit}."
    )


def _spill_pointer_line(
    rel_spill_path: str,
    *,
    need_more_than_preview: bool,
    json_shaped: bool = False,
) -> str:
    if need_more_than_preview:
        return (
            f"Full output at: {rel_spill_path} — "
            "use read_file with offset/limit only if you need more than the preview.]"
        )
    if json_shaped:
        return (
            f"Full output at: {rel_spill_path} — inlined JSON is array-capped "
            "(see the '… (+N more items)' markers); read_file the path for every item.]"
        )
    return (
        f"Full output at: {rel_spill_path} — "
        "use read_file with offset/limit to page beyond the inlined body.]"
    )


def _classify_without_preview(
    text: str,
    *,
    tool_name: str,
) -> tuple[str, bool, int, Any | None]:
    """Return ``(kind, unwrapped, body_lines, parsed)`` without building a preview.

    The preview shapers re-serialize the whole payload, which is wasted work when
    a body is being inlined instead.
    """
    body, unwrapped, parsed = _unwrap_tool_envelope(text)
    kind, _diff_paths = _spill_content_kind(body, tool_name, parsed=parsed)
    return kind, unwrapped, _line_count(body), parsed


def _json_inline_candidate(parsed: Any, budget: int) -> str | None:
    """Shaped, still-valid JSON that fits ``budget``, or None when nothing fits."""
    for cap in _JSON_INLINE_ARRAY_CAPS:
        shaped = json.dumps(
            shape_json_value(parsed, max_array_items=cap),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        if len(shaped) <= budget:
            return shaped
    return None


def _spill_inventory_note_with_kind(
    text: str,
    rel_spill_path: str,
    *,
    tool_name: str = "unknown",
) -> tuple[str, str]:
    """Return ``(inventory_note, kind)`` with deterministic preview (body-less case)."""
    kind, preview, unwrapped, body_lines = _build_spill_preview(text, tool_name=tool_name)
    header = _spill_header(
        text, tool_name=tool_name, kind=kind, unwrapped=unwrapped, body_lines=body_lines
    )
    note = "\n".join(
        [
            header,
            "Preview:",
            preview,
            _spill_pointer_line(rel_spill_path, need_more_than_preview=True),
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


def spill_inline_and_note(
    text: str,
    rel_spill_path: str,
    *,
    tool_name: str = "unknown",
    inline_budget: int,
) -> tuple[str, str]:
    """Return ``(body_prefix, note)`` for soft spill.

    When ``inline_budget > 0``, the note is preview-free (body already inlined).
    When ``inline_budget <= 0``, body_prefix is empty and the note carries a preview.
    """
    def _preview_only() -> tuple[str, str]:
        note, _kind = _spill_inventory_note_with_kind(
            text, rel_spill_path, tool_name=tool_name
        )
        return "", note

    if inline_budget <= 0:
        return _preview_only()

    kind, unwrapped, body_lines, parsed = _classify_without_preview(text, tool_name=tool_name)

    def _note(*, json_shaped: bool = False) -> str:
        return "\n".join(
            [
                _spill_header(
                    text,
                    tool_name=tool_name,
                    kind=kind,
                    unwrapped=unwrapped,
                    body_lines=body_lines,
                ),
                _spill_pointer_line(
                    rel_spill_path,
                    need_more_than_preview=False,
                    json_shaped=json_shaped,
                ),
            ]
        )

    if len(text) <= inline_budget:
        return text, _note()

    if kind == "json" and parsed is not None:
        # A raw prefix of JSON is syntactically broken; inline shaped JSON instead,
        # and fall back to the deterministic preview when even that will not fit.
        shaped = _json_inline_candidate(parsed, inline_budget)
        if shaped is None:
            return _preview_only()
        return shaped, _note(json_shaped=True)

    return text[:inline_budget], _note()


def _safe_spill_filename(call_id: str) -> str:
    safe = "".join(c for c in call_id if c.isalnum() or c in "-_")[:200]
    return safe or "call"


def _spill_dir_for_thread(workspace_root: Path, thread_id: str) -> tuple[str, Path]:
    """Return ``(relative posix dir, absolute resolved dir)`` under spill root.

    ``thread_id`` / ``session_id`` may be client-supplied; sanitize and contain
    under ``.monkeybot/spill`` so path traversal cannot escape the workspace.
    """
    safe_thread = sanitize_path_component(thread_id) or "thread"
    rel = f"{_SPILL_DIR_REL.as_posix()}/{safe_thread}"
    root = spill_root(workspace_root)
    out_dir = (Path(workspace_root) / rel).resolve()
    try:
        out_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"spill path escapes spill root: thread_id={thread_id!r}"
        ) from exc
    return rel, out_dir


def write_spill_with_inventory(
    text: str,
    workspace_root: Path,
    thread_id: str,
    call_id: str,
    *,
    tool_name: str = "unknown",
    inline_budget: int = 0,
) -> str:
    """Write raw ``text`` to spill file; return soft-spill history text.

    Always persists the full payload. When ``inline_budget > 0``, history keeps a
    body prefix plus a preview-free inventory note; otherwise history is the
    preview-carrying note only (body-less case).
    """
    rel_dir, out_dir = _spill_dir_for_thread(workspace_root, thread_id)
    filename = f"{_safe_spill_filename(call_id)}.txt"
    rel = f"{rel_dir}/{filename}"
    out_path = out_dir / filename
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    body_prefix, note = spill_inline_and_note(
        text, rel, tool_name=tool_name, inline_budget=inline_budget
    )
    history = f"{body_prefix}\n{note}" if body_prefix else note
    logger.debug(
        "tool result spilled %s",
        kv(
            tool=tool_name,
            path=rel,
            chars=len(text),
            inline_chars=len(body_prefix),
            note_chars=len(note),
        ),
    )
    return history


_TIMEOUT_PARTIAL_TAIL_CHARS = 1500


def partial_output_tail(stdout: str, stderr: str, *, max_chars: int = _TIMEOUT_PARTIAL_TAIL_CHARS) -> str:
    """Return a short tail suitable for inlining in a timeout error envelope."""
    parts: list[str] = []
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if len(combined) <= max_chars:
        return combined
    omitted = len(combined) - max_chars
    return f"…(+{omitted} chars omitted)\n{combined[-max_chars:]}"


def write_run_command_timeout_spill(
    *,
    workspace_root: Path,
    thread_id: str,
    call_id: str,
    stdout: str,
    stderr: str,
) -> str:
    """Persist drained run_command streams after timeout; return workspace-relative path."""
    body = "\n".join(
        [
            "=== run_command partial output (process killed on timeout) ===",
            "",
            "--- stdout ---",
            stdout if stdout else "(empty)",
            "",
            "--- stderr ---",
            stderr if stderr else "(empty)",
            "",
        ]
    )
    rel_dir, out_dir = _spill_dir_for_thread(workspace_root, thread_id)
    filename = f"{_safe_spill_filename(f'{call_id}-timeout')}.txt"
    rel = f"{rel_dir}/{filename}"
    out_path = out_dir / filename
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    logger.debug(
        "run_command timeout spill %s",
        kv(path=rel, stdout_chars=len(stdout), stderr_chars=len(stderr)),
    )
    return rel


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
    safe_session = sanitize_path_component(session_id) or "session"
    dirs: list[Path] = [root / safe_session]
    if root.is_dir():
        dirs.extend(sorted(root.glob(f"subagent:{safe_session}:*")))
        if safe_session != session_id and not any(
            ch in session_id for ch in GLOB_METACHARACTERS
        ):
            legacy = (root / session_id).resolve()
            try:
                legacy.relative_to(root)
            except ValueError:
                logger.warning(
                    "skipping legacy spill dir outside spill root %s",
                    kv(session_id=session_id, legacy=str(legacy)),
                )
            else:
                if legacy != root:
                    dirs.append(legacy)
    # De-dupe while preserving order; drop anything outside spill root.
    seen: set[Path] = set()
    contained: list[Path] = []
    for path in dirs:
        resolved = path.resolve()
        if resolved in seen:
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            logger.warning(
                "skipping spill dir outside spill root %s",
                kv(session_id=session_id, path=str(resolved)),
            )
            continue
        seen.add(resolved)
        contained.append(resolved)
    return contained


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
