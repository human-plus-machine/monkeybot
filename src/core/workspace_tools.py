"""LangChain tools wrapping WorkspaceFileService (same logic as /workspace/v1 HTTP API)."""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import tool

from .workspace_service import WorkspaceError, WorkspaceFileService


def _j(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def create_workspace_file_tools(
    repo_root: Path,
    *,
    settings: object | None = None,
) -> list:
    """Return five tool instances registered with the orchestrator."""
    root = Path(repo_root).resolve()
    svc = WorkspaceFileService(root, settings=settings)

    @tool
    def workspace_read_file(
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> str:
        """Read a slice of a text file under the repo root (numbered lines, total_lines, truncated).

        Paths are repo-relative (e.g. data/memory/campaigns/x/note.md). Prefer this over read_file
        for predictable paging. Use offset/limit to read more when truncated is true.
        """
        try:
            return _j(svc.read_file(path, offset=offset, limit=limit))
        except WorkspaceError as e:
            return _j({"ok": False, "error": str(e), "code": e.code})

    @tool
    def workspace_write_file(path: str, content: str = "") -> str:
        """Create or replace entire file contents (atomic write). Parent directories are created.

        Both path and content are required for real writes; content may be empty string.
        """
        try:
            return _j(svc.write_file(path, content))
        except WorkspaceError as e:
            return _j({"ok": False, "error": str(e), "code": e.code})

    @tool
    def workspace_replace_in_file(path: str, old_string: str = "", new_string: str = "") -> str:
        """Replace exactly one occurrence of old_string with new_string in a file. Fails if 0 or 2+ matches."""
        try:
            return _j(svc.replace_in_file(path, old_string, new_string))
        except WorkspaceError as e:
            return _j({"ok": False, "error": str(e), "code": e.code})

    @tool
    def workspace_glob(pattern: str, root: str | None = None) -> str:
        """List file paths matching a glob pattern under an optional repo-relative root (default repo root).

        Example pattern: **/*.md — results are capped; check truncated in JSON.
        """
        try:
            return _j(svc.glob_paths(pattern, root=root))
        except WorkspaceError as e:
            return _j({"ok": False, "error": str(e), "code": e.code})

    @tool
    def workspace_grep(
        pattern: str,
        root: str | None = None,
        ignore_case: bool = False,
        file_glob: str | None = None,
        max_matches: int | None = None,
    ) -> str:
        """Search file contents with a Python regex under optional repo-relative root.

        Optional file_glob filters by filename (e.g. *.md). Returns JSON matches [{path, line, text}, ...]; truncated if caps hit.
        """
        try:
            return _j(
                svc.grep(
                    pattern,
                    root=root,
                    ignore_case=ignore_case,
                    file_glob=file_glob,
                    max_matches=max_matches,
                )
            )
        except WorkspaceError as e:
            return _j({"ok": False, "error": str(e), "code": e.code})

    return [
        workspace_read_file,
        workspace_write_file,
        workspace_replace_in_file,
        workspace_glob,
        workspace_grep,
    ]
