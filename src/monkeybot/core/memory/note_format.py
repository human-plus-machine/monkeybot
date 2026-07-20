"""Memory note frontmatter: type + status + optional supersedes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

MemoryFolder = Literal["episodic", "semantic", "procedural", "working"]
MemoryStatus = Literal["active", "superseded", "forgotten"]

TYPED_FOLDERS: tuple[str, ...] = ("episodic", "semantic", "procedural", "working")

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass(frozen=True)
class MemoryNoteMeta:
    type: str
    status: MemoryStatus
    supersedes: str | None = None


def parse_memory_note(text: str) -> tuple[MemoryNoteMeta | None, str]:
    """Return (meta, body). Meta is None when frontmatter is missing/invalid."""
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return None, text or ""
    meta_raw = match.group("meta")
    body = match.group("body")
    fields: dict[str, str] = {}
    for line in meta_raw.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip().lower()] = val.strip()
    note_type = fields.get("type", "").strip().lower()
    status_raw = fields.get("status", "active").strip().lower()
    if status_raw not in ("active", "superseded", "forgotten"):
        return None, body
    supersedes = fields.get("supersedes") or None
    if supersedes:
        sm = _WIKI_LINK_RE.search(supersedes)
        supersedes = sm.group(1).strip() if sm else supersedes.strip()
    if not note_type:
        note_type = "episodic"
    return MemoryNoteMeta(
        type=note_type,
        status=cast(MemoryStatus, status_raw),
        supersedes=supersedes,
    ), body


def format_memory_note(
    *,
    note_type: str,
    status: MemoryStatus,
    body: str,
    supersedes: str | None = None,
    related: list[str] | None = None,
) -> str:
    lines = [
        "---",
        f"type: {note_type}",
        f"status: {status}",
    ]
    if supersedes:
        lines.append(f"supersedes: [[{supersedes}]]")
    lines.append("---")
    lines.append("")
    body_text = body.rstrip()
    related_paths = [p.replace("\\", "/").lstrip("./") for p in (related or []) if p.strip()]
    # Dedupe while preserving order; never re-state supersedes as a related link.
    seen: set[str] = set()
    related_clean: list[str] = []
    for path in related_paths:
        if path == supersedes or path in seen:
            continue
        seen.add(path)
        related_clean.append(path)
    if related_clean:
        links = " ".join(f"[[{p}]]" for p in related_clean)
        body_text = f"{body_text}\n\nRelated: {links}"
    lines.append(body_text + "\n")
    return "\n".join(lines)


def extract_memory_wiki_links(text: str) -> list[str]:
    """Return relative memory targets from ``[[...]]`` (no workspace: links)."""
    out: list[str] = []
    for match in _WIKI_LINK_RE.finditer(text or ""):
        raw = match.group(1).strip().split("|", 1)[0].strip()
        if not raw or raw.lower().startswith("workspace:"):
            continue
        path = raw.split("#", 1)[0].strip()
        if not path:
            continue
        if not path.endswith((".md", ".txt", ".markdown")):
            path = f"{path}.md"
        out.append(path.replace("\\", "/").lstrip("./"))
    return out


def folder_from_rel_path(rel: str) -> str | None:
    rel = rel.replace("\\", "/").lstrip("./")
    top = rel.split("/", 1)[0]
    return top if top in TYPED_FOLDERS else None


__all__ = [
    "TYPED_FOLDERS",
    "MemoryFolder",
    "MemoryNoteMeta",
    "MemoryStatus",
    "extract_memory_wiki_links",
    "folder_from_rel_path",
    "format_memory_note",
    "parse_memory_note",
]
