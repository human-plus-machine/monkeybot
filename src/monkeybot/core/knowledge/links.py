"""Parse Obsidian-style ``[[links]]`` from note markdown."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from monkeybot.core.knowledge.types import ParsedLink

# [[workspace:path/to/file.md#L40-72]] or [[workspace:path]] or [[notes/foo]] / [[foo]]
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_WORKSPACE_PREFIX = "workspace:"
_LINE_SPAN_RE = re.compile(r"^(?P<path>.+?)#L(?P<start>\d+)(?:-(?P<end>\d+))?$", re.IGNORECASE)
_KNOWN_NOTE_ROOTS = ("notes/", "memory/")
_NOTE_SUFFIXES = (".md", ".txt", ".markdown")


def parse_wiki_links(
    text: str,
    *,
    source_path: str | None = None,
) -> list[ParsedLink]:
    """Extract wiki links from ``text``.

    Non-``workspace:`` targets resolve relative to ``source_path``'s directory
    within the index namespace (e.g. ``memory/INDEX.md`` + ``[[episodic/x.md]]``
    → ``memory/episodic/x.md``). ``workspace:`` targets stay workspace-relative.
    """
    out: list[ParsedLink] = []
    for match in _WIKI_LINK_RE.finditer(text or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        parsed = _parse_one(raw, source_path=source_path)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_one(raw: str, *, source_path: str | None = None) -> ParsedLink | None:
    # Drop optional display alias: [[target|label]]
    target = raw.split("|", 1)[0].strip()
    if not target:
        return None

    if target.lower().startswith(_WORKSPACE_PREFIX):
        rest = target[len(_WORKSPACE_PREFIX) :].strip()
        path, start, end = _split_span(rest)
        if not path:
            return None
        normalized = _normalize_workspace_path(path)
        if normalized is None:
            return None
        return ParsedLink(
            target_path=normalized,
            link_type="workspace",
            start_line=start,
            end_line=end,
        )

    path, start, end = _split_span(target)
    if not path:
        return None

    # Bare note names → notes/{name}.md (vault convention)
    if "/" not in path and not path.endswith(_NOTE_SUFFIXES):
        normalized = _normalize_note_path(f"notes/{path}.md")
        if normalized is None:
            return None
        path = normalized
    elif path.startswith(_KNOWN_NOTE_ROOTS):
        normalized = _normalize_note_path(path)
        if normalized is None:
            return None
        path = normalized
    elif source_path:
        source_dir = PurePosixPath(source_path).parent.as_posix()
        joined = path if source_dir in ("", ".") else f"{source_dir}/{path}"
        normalized = _normalize_note_path(joined)
        if normalized is None:
            return None
        path = normalized
    elif path.endswith(_NOTE_SUFFIXES) and "/" not in path:
        normalized = _normalize_note_path(f"notes/{path}")
        if normalized is None:
            return None
        path = normalized
    else:
        # No source context and not under a known root — reject rather than
        # emit an unresolvable index path (the pre-F2 episodic/x.md bug).
        return None

    return ParsedLink(
        target_path=path,
        link_type="note",
        start_line=start,
        end_line=end,
    )


def _split_span(raw: str) -> tuple[str, int | None, int | None]:
    m = _LINE_SPAN_RE.match(raw.strip())
    if not m:
        return _strip_dot_slash(raw.strip()), None, None
    path = _strip_dot_slash(m.group("path").strip())
    start = int(m.group("start"))
    end_raw = m.group("end")
    end = int(end_raw) if end_raw else start
    if end < start:
        start, end = end, start
    return path, start, end


def _strip_dot_slash(path: str) -> str:
    """Remove leading ``./`` segments without collapsing ``../`` via lstrip."""
    while path.startswith("./"):
        path = path[2:]
    return path


def _normalize_segments(path: str) -> list[str] | None:
    """Collapse ``.`` / ``..``; return None if ``..`` escapes the path root."""
    parts: list[str] = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(seg)
    return parts


def _normalize_workspace_path(path: str) -> str | None:
    parts = _normalize_segments(path)
    if not parts:
        return None
    return "/".join(parts)


def _normalize_note_path(path: str) -> str | None:
    """Normalize and require the result stay under ``notes/`` or ``memory/``."""
    parts = _normalize_segments(path)
    if not parts:
        return None
    normalized = "/".join(parts)
    if not normalized.startswith(_KNOWN_NOTE_ROOTS):
        return None
    # Directory-only roots are not valid file targets
    if normalized in ("notes", "memory") or normalized.endswith("/"):
        return None
    return normalized


def format_link_ref(link: ParsedLink) -> str:
    """Serialize a link for hit payloads (e.g. ``workspace:path#L40-72``)."""
    if link.link_type == "workspace":
        base = f"workspace:{link.target_path}"
    else:
        base = link.target_path
    if link.start_line is not None:
        end = link.end_line if link.end_line is not None else link.start_line
        return f"{base}#L{link.start_line}-{end}"
    return base


__all__ = ["ParsedLink", "format_link_ref", "parse_wiki_links"]
