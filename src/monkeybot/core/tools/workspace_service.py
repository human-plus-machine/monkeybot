"""Safe repo-scoped read / write / replace / glob / grep for workspace API and agent tools."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from monkeybot.core.tools.text_normalize import normalize_unicode_punctuation

# Directories skipped when walking for grep: noisy, large, or not source content.
_GREP_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
    }
)


# Appended when a single line exceeds the whole char budget and had to be cut
# mid-line; the remainder is not reachable via ``offset``.
_LINE_SLICED_MARKER = " …[line cut at char budget]"


class ReadFileResult(TypedDict):
    ok: bool
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    next_offset: NotRequired[int]


class WriteFileResult(TypedDict):
    ok: bool
    path: str
    bytes: int


class ReplaceResult(TypedDict):
    ok: bool
    path: str
    replacements: int
    bytes: int
    match_mode: str


class DeleteResult(TypedDict):
    ok: bool
    path: str


class GrepMatch(TypedDict):
    path: str
    line: int
    text: str


class GlobResult(TypedDict):
    ok: bool
    root: str
    pattern: str
    paths: list[str]
    count: int
    truncated: bool
    duration_ms: int


class GrepResult(TypedDict):
    ok: bool
    root: str
    pattern: str
    matches: list[GrepMatch]
    match_count: int
    files_scanned: int
    truncated: bool
    duration_ms: int


@dataclass
class WorkspaceSettings:
    """Defaults for workspace file limits (YAML via callers, or pass explicit settings).

    The agent path always supplies YAML-derived settings, so these defaults only
    serve non-agent callers such as the gateway file-viewer endpoints, which are
    not bound by any model context window and keep the historic generous limits.
    """

    WORKSPACE_READ_MAX_LINES: int = 50_000
    WORKSPACE_READ_DEFAULT_LINES: int = 20_000
    WORKSPACE_WRITE_MAX_BYTES: int = 8_000_000
    WORKSPACE_GLOB_MAX_PATHS: int = 2000
    WORKSPACE_GLOB_TIMEOUT_SEC: float = 20.0
    WORKSPACE_GREP_MAX_MATCHES: int = 500
    WORKSPACE_GREP_MAX_FILES: int = 5000
    WORKSPACE_GREP_MAX_FILE_BYTES: int = 512_000
    # If set (repo-relative POSIX prefix, no leading slash), write_file / replace_in_file
    # only allow paths under repo_root / this prefix (e.g. sync-backed agent memory).
    WORKSPACE_WRITE_SCOPE_REL: str | None = None


class WorkspaceError(Exception):
    """Logical error for workspace operations (maps to HTTP 400)."""

    def __init__(self, message: str, code: str = "workspace_error") -> None:
        super().__init__(message)
        self.code = code


def _coerce_workspace_settings(settings: object | None) -> WorkspaceSettings:
    """Accept WorkspaceSettings, any object with same attribute names, or None."""
    if settings is None:
        return WorkspaceSettings()
    if isinstance(settings, WorkspaceSettings):
        return settings
    out = WorkspaceSettings()
    for field in (
        "WORKSPACE_READ_MAX_LINES",
        "WORKSPACE_READ_DEFAULT_LINES",
        "WORKSPACE_WRITE_MAX_BYTES",
        "WORKSPACE_GLOB_MAX_PATHS",
        "WORKSPACE_GLOB_TIMEOUT_SEC",
        "WORKSPACE_GREP_MAX_MATCHES",
        "WORKSPACE_GREP_MAX_FILES",
        "WORKSPACE_GREP_MAX_FILE_BYTES",
        "WORKSPACE_WRITE_SCOPE_REL",
    ):
        val = getattr(settings, field, None)
        if val is not None:
            setattr(out, field, val)
    return out


def _is_disproportionate_match(search: str, old_string: str) -> bool:
    """Reject fuzzy spans that are much larger than the caller's old_string."""
    old_lines = old_string.split("\n")
    search_lines = search.split("\n")
    old_n = len(old_lines)
    search_n = len(search_lines)
    if search_n >= max(old_n + 3, old_n * 2):
        return True
    if old_n == 1:
        return False
    return len(search.strip()) > max(len(old_string.strip()) + 500, len(old_string.strip()) * 4)


def _collect_exact_spans(text: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        spans.append((idx, idx + len(needle)))
        start = idx + max(len(needle), 1)
    return spans


def _line_trimmed_spans(content: str, find: str) -> list[tuple[int, int]]:
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines = search_lines[:-1]
    if not search_lines:
        return []
    spans: list[tuple[int, int]] = []
    for i in range(0, len(original_lines) - len(search_lines) + 1):
        if all(
            original_lines[i + j].strip() == search_lines[j].strip()
            for j in range(len(search_lines))
        ):
            start = sum(len(original_lines[k]) + 1 for k in range(i))
            end = start + sum(len(original_lines[i + j]) for j in range(len(search_lines)))
            if len(search_lines) > 1:
                end += len(search_lines) - 1
            spans.append((start, end))
    return spans


def _whitespace_normalized_spans(content: str, find: str) -> list[tuple[int, int]]:
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    normalized_find = norm(find)
    spans: list[tuple[int, int]] = []
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if norm(line) == normalized_find:
            start = sum(len(lines[k]) + 1 for k in range(i))
            spans.append((start, start + len(line)))
    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(0, len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i : i + len(find_lines)])
            if norm(block) == normalized_find:
                start = sum(len(lines[k]) + 1 for k in range(i))
                spans.append((start, start + len(block)))
    return spans


def _indentation_flexible_spans(content: str, find: str) -> list[tuple[int, int]]:
    def deindent(text: str) -> str:
        lines = text.split("\n")
        nonempty = [ln for ln in lines if ln.strip()]
        if not nonempty:
            return text
        min_indent = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
        return "\n".join(
            ln if not ln.strip() else ln[min_indent:] for ln in lines
        )

    normalized_find = deindent(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    spans: list[tuple[int, int]] = []
    for i in range(0, len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i : i + len(find_lines)])
        if deindent(block) == normalized_find:
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            spans.append((start, start + len(block)))
    return spans


def _unicode_normalized_spans(content: str, find: str) -> list[tuple[int, int]]:
    """Match after stripping and mapping smart quotes/dashes (shared with apply_patch)."""
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines = search_lines[:-1]
    if not search_lines:
        return []
    spans: list[tuple[int, int]] = []
    for i in range(0, len(original_lines) - len(search_lines) + 1):
        if all(
            normalize_unicode_punctuation(original_lines[i + j].strip())
            == normalize_unicode_punctuation(search_lines[j].strip())
            for j in range(len(search_lines))
        ):
            start = sum(len(original_lines[k]) + 1 for k in range(i))
            end = start + sum(len(original_lines[i + j]) for j in range(len(search_lines)))
            if len(search_lines) > 1:
                end += len(search_lines) - 1
            spans.append((start, end))
    return spans


def _find_replace_span(
    text: str,
    old_string: str,
    *,
    replace_all: bool,
) -> tuple[list[tuple[int, int]], str]:
    """Return (spans, match_mode). Raises :class:`WorkspaceError` if missing or ambiguous."""
    exact = _collect_exact_spans(text, old_string)
    if exact:
        if len(exact) > 1 and not replace_all:
            raise WorkspaceError(
                "old_string is not unique; widen the snippet or set replace_all=true",
                code="ambiguous_replace",
            )
        return (exact if replace_all else exact[:1]), "exact"

    for mode, finder in (
        ("line_trimmed", _line_trimmed_spans),
        ("whitespace_normalized", _whitespace_normalized_spans),
        ("indentation_flexible", _indentation_flexible_spans),
        ("unicode_normalized", _unicode_normalized_spans),
    ):
        spans = finder(text, old_string)
        if not spans:
            continue
        kept: list[tuple[int, int]] = []
        for start, end in spans:
            matched = text[start:end]
            if _is_disproportionate_match(matched, old_string):
                continue
            kept.append((start, end))
        if not kept:
            continue
        if len(kept) > 1 and not replace_all:
            raise WorkspaceError(
                "old_string is not unique; widen the snippet or set replace_all=true",
                code="ambiguous_replace",
            )
        return (kept if replace_all else kept[:1]), mode

    raise WorkspaceError("old_string not found in file", code="not_found_replace")


def _apply_replace_spans(
    text: str,
    spans: list[tuple[int, int]],
    new_string: str,
) -> str:
    """Apply replacements from end to start so offsets stay valid."""
    out = text
    for start, end in sorted(spans, key=lambda s: s[0], reverse=True):
        out = out[:start] + new_string + out[end:]
    return out


class WorkspaceFileService:
    """Virtual workspace paths with a separately mounted read-only ``skills/`` root."""

    def __init__(
        self,
        repo_root: Path,
        settings: object | None = None,
        *,
        skills_root: Path | None = None,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._skills_root = Path(skills_root).resolve() if skills_root is not None else None
        self._settings = _coerce_workspace_settings(settings)

    @property
    def repo_root(self) -> Path:
        return self._root

    @property
    def skills_root(self) -> Path | None:
        return self._skills_root

    @staticmethod
    def _normalize_rel_segments(rel: str, *, label: str) -> tuple[str, ...]:
        """Split a repo-relative path, normalize ``.`` / ``..``, forbid absolute paths."""
        stack: list[str] = []
        for part in rel.replace("\\", "/").split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                if not stack:
                    raise WorkspaceError(
                        f"Invalid {label}: path escapes workspace root",
                        code="invalid_path",
                    )
                stack.pop()
            else:
                stack.append(part)
        return tuple(stack)

    def _path_root_and_segments(self, segments: tuple[str, ...]) -> tuple[Path, tuple[str, ...]]:
        if segments[:1] == ("skills",) and self._skills_root is not None:
            return self._skills_root, segments[1:]
        return self._root, segments

    @staticmethod
    def _assert_realpath_under(path: Path, root: Path, *, label: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise WorkspaceError(f"Invalid {label}: path escapes its root", code="path_escape") from exc
        return path

    def _join_under_root(self, segments: tuple[str, ...], *, label: str) -> Path:
        root, child_segments = self._path_root_and_segments(segments)
        candidate = root.joinpath(*child_segments) if child_segments else root
        return self._assert_realpath_under(candidate, root, label=label)

    def _is_skills_path(self, rel: str) -> bool:
        return bool(self._normalize_rel_segments(rel, label="path")[:1] == ("skills",))

    def _require_writable_path(self, rel: str) -> None:
        if self._is_skills_path(rel):
            raise WorkspaceError("skills are read-only", code="skills_read_only")

    def require_writable_path(self, rel: str) -> Path:
        """Validate a virtual path is writable and return its safe physical path."""
        self._require_writable_path(rel)
        return self._resolve_under_root(rel)

    def _resolve_under_root(self, rel: str, *, label: str = "path") -> Path:
        if rel is None or not str(rel).strip():
            raise WorkspaceError(f"{label} is required", code="missing_path")
        s = str(rel).strip().replace("\\", "/")
        if s.startswith("~") or s.startswith("/"):
            raise WorkspaceError(f"Invalid {label}: absolute or home not allowed", code="invalid_path")
        segs = self._normalize_rel_segments(s.lstrip("/"), label=label)
        return self._join_under_root(segs, label=label)

    def resolve_workspace_path(self, rel: str, *, label: str = "path") -> Path:
        """Public path preflight: repo-relative → absolute path under the workspace root."""
        return self._resolve_under_root(rel, label=label)

    def _resolve_root_dir(self, rel: str | None) -> Path:
        if rel is None or not str(rel).strip() or str(rel).strip() in (".", "./"):
            return self._root
        s = str(rel).strip().replace("\\", "/")
        if s.startswith("~") or s.startswith("/"):
            raise WorkspaceError("Invalid root: absolute or home not allowed", code="invalid_path")
        segs = self._normalize_rel_segments(s.lstrip("/"), label="root")
        return self._join_under_root(segs, label="root")

    def _write_scope_root(self) -> Path | None:
        rel = self._settings.WORKSPACE_WRITE_SCOPE_REL
        if rel is None:
            return None
        s = str(rel).strip().replace("\\", "/").lstrip("/")
        if not s or ".." in s:
            return None
        return (self._root / s).resolve()

    def _require_under_write_scope(self, fp: Path) -> None:
        scope_root = self._write_scope_root()
        if scope_root is None:
            return
        monkeybot_root = (self._root / ".monkeybot").resolve()
        try:
            fp.resolve().relative_to(monkeybot_root)
            return
        except ValueError:
            pass
        try:
            fp.resolve().relative_to(scope_root)
        except ValueError:
            raise WorkspaceError(
                f"Writes are limited to {self._settings.WORKSPACE_WRITE_SCOPE_REL!r} (repo-relative)",
                code="write_outside_scope",
            ) from None

    def list_directory(self, path: str | None) -> list[dict[str, str]]:
        """List immediate children of a repo-relative directory (dirs first, then files).

        ``path`` may be ``None``, empty, or ``"."`` for the workspace root.
        """
        base = self._resolve_root_dir(path)
        if not base.is_dir():
            raise WorkspaceError(f"Not a directory: {path or '.'}", code="not_found")
        try:
            children = list(base.iterdir())
        except OSError as e:
            raise WorkspaceError(f"List failed: {e}", code="list_failed") from e

        def sort_key(p: Path) -> tuple[int, str]:
            return (0 if p.is_dir() else 1, p.name.lower())

        out: list[dict[str, str]] = []
        for ch in sorted(children, key=sort_key):
            if ch.name in (".", ".."):
                continue
            full = base / ch.name
            try:
                rel_p = self._as_repo_rel(full)
            except WorkspaceError:
                continue
            try:
                kind = "dir" if full.is_dir() else "file"
            except OSError:
                continue
            out.append({"name": ch.name, "path": rel_p, "kind": kind})
        return out

    def read_file(
        self,
        path: str,
        *,
        offset: int = 1,
        limit: int | None = None,
        max_chars: int | None = None,
        apply_default_limit: bool = True,
    ) -> ReadFileResult:
        if offset < 1:
            raise WorkspaceError("offset must be >= 1", code="invalid_offset")
        max_lines = self._settings.WORKSPACE_READ_MAX_LINES
        if limit is None:
            if apply_default_limit:
                limit = self._settings.WORKSPACE_READ_DEFAULT_LINES
        elif limit < 1:
            raise WorkspaceError(
                f"limit must be between 1 and {max_lines}",
                code="invalid_limit",
            )
        elif limit > max_lines:
            # Char budget is the authority when present; otherwise clamp.
            limit = max_lines
        fp = self._resolve_under_root(path)
        if not fp.is_file():
            raise WorkspaceError(f"Not a file: {path}", code="not_found")
        text = fp.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        start_idx = offset - 1
        if start_idx > total:
            return {
                "ok": True,
                "path": self._as_repo_rel(fp),
                "content": "",
                "start_line": offset,
                "end_line": offset - 1,
                "total_lines": total,
                "truncated": False,
            }
        end_cap = total if limit is None else min(start_idx + limit, total)

        width = max(6, len(str(max(total, 1))))
        if max_chars is None:
            end_idx = end_cap
            chunk = lines[start_idx:end_idx]
            numbered = "\n".join(
                f"{start_idx + 1 + i:{width}d}|{chunk[i]}" for i in range(len(chunk))
            )
            truncated = end_idx < total
            result: ReadFileResult = {
                "ok": True,
                "path": self._as_repo_rel(fp),
                "content": numbered,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "total_lines": total,
                "truncated": truncated,
            }
            if truncated:
                result["next_offset"] = end_idx + 1
            return result

        # Char-bounded selection: accumulate numbered line length; never chop mid-slice.
        selected: list[str] = []
        used = 0
        end_idx = start_idx
        sliced = False
        for i in range(start_idx, end_cap):
            line = lines[i]
            numbered_line = f"{i + 1:{width}d}|{line}"
            add = len(numbered_line) + (1 if selected else 0)
            if selected and used + add > max_chars:
                break
            if not selected and add > max_chars:
                # Always advance at least one line so paging cannot deadlock. The
                # remainder of this line is unreachable by offset, so say so.
                kept = numbered_line[: max(1, max_chars)]
                selected.append(f"{kept}{_LINE_SLICED_MARKER}")
                end_idx = i + 1
                sliced = True
                break
            selected.append(numbered_line)
            used += add
            end_idx = i + 1

        more_lines = end_idx < total
        out: ReadFileResult = {
            "ok": True,
            "path": self._as_repo_rel(fp),
            "content": "\n".join(selected),
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "total_lines": total,
            "truncated": more_lines or sliced,
        }
        if more_lines:
            out["next_offset"] = end_idx + 1
        return out

    def write_file(self, path: str, content: str) -> WriteFileResult:
        if content is None:
            content = ""
        raw = content.encode("utf-8")
        if len(raw) > self._settings.WORKSPACE_WRITE_MAX_BYTES:
            raise WorkspaceError(
                f"Content exceeds WORKSPACE_WRITE_MAX_BYTES ({self._settings.WORKSPACE_WRITE_MAX_BYTES})",
                code="payload_too_large",
            )
        self._require_writable_path(path)
        fp = self._resolve_under_root(path)
        self._require_under_write_scope(fp)
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_suffix(fp.suffix + ".workspace_tmp")
        try:
            tmp.write_bytes(raw)
            tmp.replace(fp)
        except OSError as e:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise WorkspaceError(f"Write failed: {e}", code="write_failed") from e
        return {
            "ok": True,
            "path": self._as_repo_rel(fp),
            "bytes": len(raw),
        }

    def delete_file(self, path: str) -> DeleteResult:
        self._require_writable_path(path)
        fp = self._resolve_under_root(path)
        self._require_under_write_scope(fp)
        if not fp.exists():
            raise WorkspaceError(f"Not a file: {path}", code="not_found")
        if fp.is_dir():
            raise WorkspaceError(f"Path is a directory, not a file: {path}", code="is_directory")
        if not fp.is_file():
            raise WorkspaceError(f"Not a file: {path}", code="not_found")
        try:
            fp.unlink()
        except OSError as e:
            raise WorkspaceError(f"Delete failed: {e}", code="delete_failed") from e
        return {"ok": True, "path": self._as_repo_rel(fp)}

    def replace_in_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> ReplaceResult:
        if old_string is None:
            old_string = ""
        if new_string is None:
            new_string = ""
        if old_string == new_string:
            raise WorkspaceError(
                "No changes to apply: old_string and new_string are identical.",
                code="no_change",
            )
        if old_string == "":
            raise WorkspaceError(
                "old_string cannot be empty when editing an existing file. "
                "Provide the exact text to replace, or use write_file for a full rewrite.",
                code="empty_old_string",
            )
        self._require_writable_path(path)
        fp = self._resolve_under_root(path)
        self._require_under_write_scope(fp)
        if not fp.is_file():
            raise WorkspaceError(f"Not a file: {path}", code="not_found")
        text = fp.read_text(encoding="utf-8", errors="replace")
        spans, match_mode = _find_replace_span(text, old_string, replace_all=replace_all)
        new_text = _apply_replace_spans(text, spans, new_string)
        raw = new_text.encode("utf-8")
        if len(raw) > self._settings.WORKSPACE_WRITE_MAX_BYTES:
            raise WorkspaceError(
                f"Result exceeds WORKSPACE_WRITE_MAX_BYTES ({self._settings.WORKSPACE_WRITE_MAX_BYTES})",
                code="payload_too_large",
            )
        tmp = fp.with_suffix(fp.suffix + ".workspace_tmp")
        try:
            tmp.write_bytes(raw)
            tmp.replace(fp)
        except OSError as e:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise WorkspaceError(f"Write failed: {e}", code="write_failed") from e
        return {
            "ok": True,
            "path": self._as_repo_rel(fp),
            "replacements": len(spans),
            "bytes": len(raw),
            "match_mode": match_mode,
        }

    def glob_paths(self, pattern: str, root: str | None = None) -> GlobResult:
        if not pattern or not pattern.strip():
            raise WorkspaceError("pattern is required", code="missing_pattern")
        pattern = pattern.strip()
        if pattern.startswith("/") or ".." in pattern:
            raise WorkspaceError("Invalid glob pattern", code="invalid_pattern")
        # ``glob("skills/**/*.md")`` follows the same virtual routing rule as
        # read_file; callers may alternatively pass ``root="skills"``.
        effective_pattern = pattern
        effective_root = root
        if root is None and self._skills_root is not None and pattern.startswith("skills/"):
            effective_root = "skills"
            effective_pattern = pattern.removeprefix("skills/") or "**/*"
        base = self._resolve_root_dir(effective_root)
        deadline = time.monotonic() + self._settings.WORKSPACE_GLOB_TIMEOUT_SEC
        max_paths = self._settings.WORKSPACE_GLOB_MAX_PATHS
        paths: list[str] = []
        truncated = False
        t0 = time.monotonic()
        try:
            for p in base.glob(effective_pattern):
                if time.monotonic() > deadline:
                    truncated = True
                    break
                if not p.is_file():
                    continue
                try:
                    self._as_repo_rel(p)
                except WorkspaceError:
                    continue
                paths.append(self._as_repo_rel(p))
                if len(paths) >= max_paths:
                    truncated = True
                    break
        except OSError as e:
            raise WorkspaceError(f"Glob failed: {e}", code="glob_failed") from e
        paths.sort()
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "root": self._as_repo_rel(base) if base != self._root else ".",
            "pattern": pattern,
            "paths": paths,
            "count": len(paths),
            "truncated": truncated,
            "duration_ms": duration_ms,
        }

    def grep(
        self,
        pattern: str,
        *,
        root: str | None = None,
        ignore_case: bool = False,
        file_glob: str | None = None,
        max_matches: int | None = None,
    ) -> GrepResult:
        if not pattern or not str(pattern).strip():
            raise WorkspaceError("pattern is required", code="missing_pattern")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern.strip(), flags)
        except re.error as e:
            raise WorkspaceError(f"Invalid regex: {e}", code="invalid_regex") from e
        base = self._resolve_root_dir(root)
        max_m = max_matches if max_matches is not None else self._settings.WORKSPACE_GREP_MAX_MATCHES
        max_files = self._settings.WORKSPACE_GREP_MAX_FILES
        max_file_bytes = self._settings.WORKSPACE_GREP_MAX_FILE_BYTES
        matches: list[GrepMatch] = []
        files_scanned = 0
        truncated = False
        t0 = time.monotonic()
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _GREP_IGNORE_DIRS)
            for name in sorted(filenames):
                if len(matches) >= max_m:
                    truncated = True
                    break
                if files_scanned >= max_files:
                    truncated = True
                    break
                fp = Path(dirpath) / name
                try:
                    rel = self._as_repo_rel(fp)
                except WorkspaceError:
                    continue
                if file_glob and not fnmatch.fnmatch(fp.name, file_glob):
                    continue
                try:
                    st = fp.stat()
                except OSError:
                    continue
                if st.st_size > max_file_bytes:
                    continue
                files_scanned += 1
                try:
                    data = fp.read_bytes()
                except OSError:
                    continue
                if b"\x00" in data[:8192]:
                    continue
                text = data.decode("utf-8", errors="replace")
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if len(matches) >= max_m:
                        truncated = True
                        break
                    if regex.search(line):
                        matches.append(
                            {
                                "path": rel,
                                "line": line_no,
                                "text": line[:2000],
                            }
                        )
                if truncated:
                    break
            if truncated:
                break
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "root": self._as_repo_rel(base) if base != self._root else ".",
            "pattern": pattern.strip(),
            "matches": matches,
            "match_count": len(matches),
            "files_scanned": files_scanned,
            "truncated": truncated,
            "duration_ms": duration_ms,
        }

    def _as_repo_rel(self, p: Path) -> str:
        if self._skills_root is not None:
            try:
                rel = p.resolve().relative_to(self._skills_root.resolve()).as_posix()
                return "skills" if rel == "." else f"skills/{rel}"
            except ValueError:
                pass
        try:
            rel = p.relative_to(self._root).as_posix()
            return rel or "."
        except ValueError:
            pass
        try:
            rel = p.resolve().relative_to(self._root).as_posix()
            return rel or "."
        except ValueError as e:
            raise WorkspaceError("path escapes workspace root", code="path_escape") from e
