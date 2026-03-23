"""Safe repo-scoped read / write / replace / glob / grep for workspace API and agent tools."""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorkspaceSettings:
    """Defaults for workspace file limits (override via env in callers or pass explicit settings)."""

    WORKSPACE_READ_MAX_LINES: int = 500
    WORKSPACE_READ_DEFAULT_LINES: int = 200
    WORKSPACE_WRITE_MAX_BYTES: int = 8_000_000
    WORKSPACE_GLOB_MAX_PATHS: int = 2000
    WORKSPACE_GLOB_TIMEOUT_SEC: float = 20.0
    WORKSPACE_GREP_MAX_MATCHES: int = 500
    WORKSPACE_GREP_MAX_FILES: int = 5000
    WORKSPACE_GREP_MAX_FILE_BYTES: int = 512_000


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
    ):
        val = getattr(settings, field, None)
        if val is not None:
            setattr(out, field, val)
    return out


class WorkspaceFileService:
    """All paths are repo-relative POSIX strings; resolved under ``repo_root``."""

    def __init__(self, repo_root: Path, settings: object | None = None) -> None:
        self._root = Path(repo_root).resolve()
        self._settings = _coerce_workspace_settings(settings)

    @property
    def repo_root(self) -> Path:
        return self._root

    def _resolve_under_root(self, rel: str, *, label: str = "path") -> Path:
        if rel is None or not str(rel).strip():
            raise WorkspaceError(f"{label} is required", code="missing_path")
        s = str(rel).strip().replace("\\", "/")
        if ".." in s or s.startswith("~"):
            raise WorkspaceError(f"Invalid {label}: traversal or home not allowed", code="invalid_path")
        s = s.lstrip("/")
        candidate = (self._root / s).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            raise WorkspaceError(f"{label} escapes workspace root", code="path_escape") from None
        return candidate

    def _resolve_root_dir(self, rel: str | None) -> Path:
        if rel is None or not str(rel).strip() or str(rel).strip() in (".", "./"):
            return self._root
        return self._resolve_under_root(str(rel).strip(), label="root")

    def read_file(
        self,
        path: str,
        *,
        offset: int = 1,
        limit: int | None = None,
    ) -> dict:
        if offset < 1:
            raise WorkspaceError("offset must be >= 1", code="invalid_offset")
        max_lines = self._settings.WORKSPACE_READ_MAX_LINES
        if limit is None:
            limit = self._settings.WORKSPACE_READ_DEFAULT_LINES
        if limit < 1 or limit > max_lines:
            raise WorkspaceError(
                f"limit must be between 1 and {max_lines}",
                code="invalid_limit",
            )
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
        end_idx = min(start_idx + limit, total)
        chunk = lines[start_idx:end_idx]
        width = max(6, len(str(end_idx)))
        numbered = "\n".join(f"{start_idx + 1 + i:{width}d}|{chunk[i]}" for i in range(len(chunk)))
        return {
            "ok": True,
            "path": self._as_repo_rel(fp),
            "content": numbered,
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "total_lines": total,
            "truncated": end_idx < total,
        }

    def write_file(self, path: str, content: str) -> dict:
        if content is None:
            content = ""
        raw = content.encode("utf-8")
        if len(raw) > self._settings.WORKSPACE_WRITE_MAX_BYTES:
            raise WorkspaceError(
                f"Content exceeds WORKSPACE_WRITE_MAX_BYTES ({self._settings.WORKSPACE_WRITE_MAX_BYTES})",
                code="payload_too_large",
            )
        fp = self._resolve_under_root(path)
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

    def replace_in_file(self, path: str, old_string: str, new_string: str) -> dict:
        if old_string is None:
            old_string = ""
        if new_string is None:
            new_string = ""
        fp = self._resolve_under_root(path)
        if not fp.is_file():
            raise WorkspaceError(f"Not a file: {path}", code="not_found")
        text = fp.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_string)
        if count == 0:
            raise WorkspaceError("old_string not found in file", code="not_found_replace")
        if count > 1:
            raise WorkspaceError(
                f"old_string is not unique ({count} matches); widen the snippet",
                code="ambiguous_replace",
            )
        new_text = text.replace(old_string, new_string, 1)
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
            "replacements": 1,
            "bytes": len(raw),
        }

    def glob_paths(self, pattern: str, root: str | None = None) -> dict:
        if not pattern or not pattern.strip():
            raise WorkspaceError("pattern is required", code="missing_pattern")
        pattern = pattern.strip()
        if pattern.startswith("/") or ".." in pattern:
            raise WorkspaceError("Invalid glob pattern", code="invalid_pattern")
        base = self._resolve_root_dir(root)
        deadline = time.monotonic() + self._settings.WORKSPACE_GLOB_TIMEOUT_SEC
        max_paths = self._settings.WORKSPACE_GLOB_MAX_PATHS
        paths: list[str] = []
        truncated = False
        t0 = time.monotonic()
        try:
            for p in base.glob(pattern):
                if time.monotonic() > deadline:
                    truncated = True
                    break
                if not p.is_file():
                    continue
                try:
                    p.resolve().relative_to(self._root)
                except ValueError:
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
    ) -> dict:
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
        matches: list[dict] = []
        files_scanned = 0
        truncated = False
        t0 = time.monotonic()
        for fp in sorted(base.rglob("*")):
            if len(matches) >= max_m:
                truncated = True
                break
            if files_scanned >= max_files:
                truncated = True
                break
            if not fp.is_file():
                continue
            try:
                fp.resolve().relative_to(self._root)
            except ValueError:
                continue
            rel = self._as_repo_rel(fp)
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
        rel = p.resolve().relative_to(self._root)
        return rel.as_posix()
