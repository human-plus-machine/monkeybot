"""Codex-style ``apply_patch`` parse + derive + fail-closed apply.

Format (OpenCode / Codex envelope)::

    *** Begin Patch
    *** Add File: path
    +line
    *** Update File: path
    *** Move to: new_path   # optional
    @@ context
     keep
    -old
    +new
    *** Delete File: path
    *** End Patch
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from monkeybot.core.logging_utils import kv
from monkeybot.core.tools.workspace_service import WorkspaceError

if TYPE_CHECKING:
    from monkeybot.core.tools.workspace_service import WorkspaceFileService

logger = logging.getLogger(__name__)

BEGIN_MARKER = "*** Begin Patch"
END_MARKER = "*** End Patch"


class PatchError(Exception):
    """Patch parse or apply validation failure (fail-closed; no disk writes yet)."""

    def __init__(self, message: str, code: str = "patch_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UpdateChunk:
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    change_context: str | None = None
    is_end_of_file: bool = False


@dataclass(frozen=True)
class AddHunk:
    path: str
    contents: str


@dataclass(frozen=True)
class DeleteHunk:
    path: str


@dataclass(frozen=True)
class UpdateHunk:
    path: str
    chunks: tuple[UpdateChunk, ...]
    move_path: str | None = None


Hunk = AddHunk | DeleteHunk | UpdateHunk


def _strip_heredoc(input_text: str) -> str:
    match = re.match(
        r"^(?:cat\s+)?<<['\"]?(\w+)['\"]?\s*\n([\s\S]*?)\n\1\s*$",
        input_text,
    )
    if match:
        return match.group(2)
    return input_text


def _parse_header(
    lines: list[str], start_idx: int
) -> tuple[Literal["add", "delete", "update"], str, str | None, int] | None:
    """Return (kind, file_path, move_path, next_idx) or None."""
    line = lines[start_idx]
    if line.startswith("*** Add File:"):
        path = line[len("*** Add File:") :].strip()
        return ("add", path, None, start_idx + 1) if path else None
    if line.startswith("*** Delete File:"):
        path = line[len("*** Delete File:") :].strip()
        return ("delete", path, None, start_idx + 1) if path else None
    if line.startswith("*** Update File:"):
        path = line[len("*** Update File:") :].strip()
        move_path: str | None = None
        next_idx = start_idx + 1
        if next_idx < len(lines) and lines[next_idx].startswith("*** Move to:"):
            move_path = lines[next_idx][len("*** Move to:") :].strip() or None
            next_idx += 1
        return ("update", path, move_path, next_idx) if path else None
    return None


def _parse_add_content(lines: list[str], start_idx: int) -> tuple[str, int]:
    parts: list[str] = []
    i = start_idx
    while i < len(lines) and not lines[i].startswith("***"):
        if lines[i].startswith("+"):
            parts.append(lines[i][1:])
        i += 1
    return "\n".join(parts), i


def _parse_update_chunks(lines: list[str], start_idx: int) -> tuple[list[UpdateChunk], int]:
    chunks: list[UpdateChunk] = []
    i = start_idx
    while i < len(lines) and not lines[i].startswith("***"):
        if not lines[i].startswith("@@"):
            i += 1
            continue
        context_line = lines[i][2:].strip()
        i += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        is_eof = False
        while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("***"):
            change = lines[i]
            if change == "*** End of File":
                is_eof = True
                i += 1
                break
            if change.startswith(" "):
                content = change[1:]
                old_lines.append(content)
                new_lines.append(content)
            elif change.startswith("-"):
                old_lines.append(change[1:])
            elif change.startswith("+"):
                new_lines.append(change[1:])
            i += 1
        chunks.append(
            UpdateChunk(
                old_lines=tuple(old_lines),
                new_lines=tuple(new_lines),
                change_context=context_line or None,
                is_end_of_file=is_eof,
            )
        )
    return chunks, i


def parse_patch(patch_text: str) -> list[Hunk]:
    """Parse a Codex-style patch into hunks. Raises :class:`PatchError` on invalid input."""
    cleaned = _strip_heredoc(patch_text.strip())
    lines = cleaned.split("\n")
    try:
        begin_idx = next(i for i, ln in enumerate(lines) if ln.strip() == BEGIN_MARKER)
        end_idx = next(i for i, ln in enumerate(lines) if ln.strip() == END_MARKER)
    except StopIteration as exc:
        raise PatchError(
            "Invalid patch format: missing Begin/End markers",
            code="missing_markers",
        ) from exc
    if begin_idx >= end_idx:
        raise PatchError(
            "Invalid patch format: Begin marker must precede End marker",
            code="missing_markers",
        )

    hunks: list[Hunk] = []
    i = begin_idx + 1
    while i < end_idx:
        header = _parse_header(lines, i)
        if header is None:
            i += 1
            continue
        kind, path, move_path, next_idx = header
        if kind == "add":
            content, i = _parse_add_content(lines, next_idx)
            hunks.append(AddHunk(path=path, contents=content))
        elif kind == "delete":
            hunks.append(DeleteHunk(path=path))
            i = next_idx
        else:
            chunks, i = _parse_update_chunks(lines, next_idx)
            hunks.append(UpdateHunk(path=path, move_path=move_path, chunks=tuple(chunks)))

    if not hunks:
        raise PatchError("patch rejected: empty patch (no hunks)", code="empty_patch")
    return hunks


def _normalize_unicode(s: str) -> str:
    return (
        s.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201a", "'")
        .replace("\u201b", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201e", '"')
        .replace("\u201f", '"')
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u2026", "...")
        .replace("\u00a0", " ")
    )


def _try_match(
    lines: list[str],
    pattern: list[str],
    start_index: int,
    compare: Callable[[str, str], bool],
    eof: bool,
) -> int:
    if eof:
        from_end = len(lines) - len(pattern)
        if from_end >= start_index and all(
            compare(lines[from_end + j], pattern[j]) for j in range(len(pattern))
        ):
            return from_end
    for i in range(start_index, len(lines) - len(pattern) + 1):
        if all(compare(lines[i + j], pattern[j]) for j in range(len(pattern))):
            return i
    return -1


def seek_sequence(
    lines: list[str],
    pattern: list[str],
    start_index: int = 0,
    *,
    eof: bool = False,
) -> int:
    """Find ``pattern`` in ``lines`` with exact → rstrip → trim → unicode-normalize passes."""
    if not pattern:
        return -1
    for compare in (
        (lambda a, b: a == b),
        (lambda a, b: a.rstrip() == b.rstrip()),
        (lambda a, b: a.strip() == b.strip()),
        (lambda a, b: _normalize_unicode(a.strip()) == _normalize_unicode(b.strip())),
    ):
        found = _try_match(lines, pattern, start_index, compare, eof)
        if found != -1:
            return found
    return -1


def derive_new_contents(
    file_path: str,
    chunks: tuple[UpdateChunk, ...] | list[UpdateChunk],
    original_text: str,
) -> str:
    """Apply update chunks to ``original_text``; raise :class:`PatchError` if a chunk cannot match."""
    original_lines = original_text.split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines = original_lines[:-1]

    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context:
            ctx_idx = seek_sequence(original_lines, [chunk.change_context], line_index)
            if ctx_idx == -1:
                raise PatchError(
                    f"Failed to find context '{chunk.change_context}' in {file_path}",
                    code="context_not_found",
                )
            line_index = ctx_idx + 1

        if not chunk.old_lines:
            insertion_idx = (
                len(original_lines) - 1
                if original_lines and original_lines[-1] == ""
                else len(original_lines)
            )
            replacements.append((insertion_idx, 0, list(chunk.new_lines)))
            continue

        pattern = list(chunk.old_lines)
        new_slice = list(chunk.new_lines)
        found = seek_sequence(
            original_lines, pattern, line_index, eof=chunk.is_end_of_file
        )
        if found == -1 and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = seek_sequence(
                original_lines, pattern, line_index, eof=chunk.is_end_of_file
            )
        if found == -1:
            raise PatchError(
                f"Failed to find expected lines in {file_path}:\n"
                + "\n".join(chunk.old_lines),
                code="hunk_not_found",
            )
        replacements.append((found, len(pattern), new_slice))
        line_index = found + len(pattern)

    replacements.sort(key=lambda r: r[0])
    result = list(original_lines)
    for start_idx, old_len, new_segment in reversed(replacements):
        del result[start_idx : start_idx + old_len]
        for j, line in enumerate(new_segment):
            result.insert(start_idx + j, line)

    if not result or result[-1] != "":
        result.append("")
    return "\n".join(result)


@dataclass(frozen=True)
class _PlannedOp:
    action: Literal["add", "update", "move", "delete"]
    path: str
    content: str | None = None
    move_path: str | None = None
    # Prior file body for rollback after a mid-apply failure.
    old_content: str | None = None


def _plan_ops(workspace: WorkspaceFileService, hunks: list[Hunk]) -> list[_PlannedOp]:
    if not hunks:
        raise PatchError("patch rejected: empty patch (no hunks)", code="empty_patch")

    planned: list[_PlannedOp] = []
    for hunk in hunks:
        if isinstance(hunk, AddHunk):
            workspace.resolve_workspace_path(hunk.path)
            content = hunk.contents
            if content and not content.endswith("\n"):
                content = content + "\n"
            planned.append(_PlannedOp(action="add", path=hunk.path, content=content))
        elif isinstance(hunk, DeleteHunk):
            fp = workspace.resolve_workspace_path(hunk.path)
            if not fp.is_file():
                raise PatchError(
                    f"apply_patch verification failed: Failed to read file to delete: {hunk.path}",
                    code="not_found",
                )
            old = fp.read_text(encoding="utf-8", errors="replace")
            planned.append(
                _PlannedOp(action="delete", path=hunk.path, old_content=old)
            )
        else:
            fp = workspace.resolve_workspace_path(hunk.path)
            if not fp.is_file():
                raise PatchError(
                    f"apply_patch verification failed: Failed to read file to update: {hunk.path}",
                    code="not_found",
                )
            if hunk.move_path:
                workspace.resolve_workspace_path(hunk.move_path)
            old = fp.read_text(encoding="utf-8", errors="replace")
            new_content = derive_new_contents(hunk.path, hunk.chunks, old)
            if hunk.move_path:
                planned.append(
                    _PlannedOp(
                        action="move",
                        path=hunk.path,
                        move_path=hunk.move_path,
                        content=new_content,
                        old_content=old,
                    )
                )
            else:
                planned.append(
                    _PlannedOp(
                        action="update",
                        path=hunk.path,
                        content=new_content,
                        old_content=old,
                    )
                )
    return planned


def _rollback_ops(workspace: WorkspaceFileService, done: list[_PlannedOp]) -> None:
    """Best-effort undo of successfully applied ops (reverse order)."""
    for op in reversed(done):
        try:
            if op.action == "add":
                workspace.delete_file(op.path)
            elif op.action == "update":
                if op.old_content is not None:
                    workspace.write_file(op.path, op.old_content)
            elif op.action == "move":
                if op.move_path is not None:
                    try:
                        workspace.delete_file(op.move_path)
                    except WorkspaceError as exc:
                        logger.warning(
                            "apply_patch rollback: could not remove move dest %s",
                            kv(path=op.move_path, error=str(exc)),
                        )
                if op.old_content is not None:
                    workspace.write_file(op.path, op.old_content)
            elif op.action == "delete":
                if op.old_content is not None:
                    workspace.write_file(op.path, op.old_content)
        except WorkspaceError:
            logger.exception(
                "apply_patch rollback failed %s",
                kv(action=op.action, path=op.path, move_path=op.move_path),
            )


def _apply_ops(
    workspace: WorkspaceFileService, planned: list[_PlannedOp]
) -> list[dict[str, object]]:
    done: list[_PlannedOp] = []
    files: list[dict[str, object]] = []
    try:
        for op in planned:
            if op.action == "add":
                if op.content is None:
                    raise PatchError("add op missing content", code="internal")
                workspace.write_file(op.path, op.content)
                files.append({"path": op.path, "action": "add"})
            elif op.action == "update":
                if op.content is None:
                    raise PatchError("update op missing content", code="internal")
                workspace.write_file(op.path, op.content)
                files.append({"path": op.path, "action": "update"})
            elif op.action == "move":
                if op.content is None or op.move_path is None:
                    raise PatchError("move op missing content or move_path", code="internal")
                workspace.write_file(op.move_path, op.content)
                workspace.delete_file(op.path)
                files.append(
                    {"path": op.move_path, "action": "move", "from": op.path}
                )
            elif op.action == "delete":
                workspace.delete_file(op.path)
                files.append({"path": op.path, "action": "delete"})
            done.append(op)
    except (WorkspaceError, PatchError) as exc:
        _rollback_ops(workspace, done)
        if isinstance(exc, PatchError):
            raise
        raise PatchError(str(exc), code=getattr(exc, "code", "write_failed")) from exc
    return files


def _file_summary_line(entry: dict[str, object]) -> str:
    action = str(entry["action"])
    path = str(entry["path"])
    if action == "move":
        return f"M {entry.get('from')} -> {path}"
    return f"{action[0].upper()} {path}"


def plan_and_apply_patch(
    workspace: WorkspaceFileService,
    hunks: list[Hunk],
) -> dict[str, object]:
    """Validate all hunks, then apply; roll back on mid-apply failure."""
    planned = _plan_ops(workspace, hunks)
    try:
        files = _apply_ops(workspace, planned)
    except PatchError as exc:
        logger.warning(
            "apply_patch failed %s",
            kv(code=exc.code, hunks=len(hunks), error=str(exc)),
        )
        raise

    summary = "\n".join(_file_summary_line(f) for f in files)
    logger.info(
        "apply_patch ok %s",
        kv(files=len(files), actions=",".join(str(f["action"]) for f in files)),
    )
    return {
        "ok": True,
        "files": files,
        "message": f"Success. Updated the following files:\n{summary}",
    }
