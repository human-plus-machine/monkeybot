"""Playbook storage under a single directory; host slugs only (no path traversal)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse


class PlaybookError(ValueError):
    """Invalid host or playbook path."""


def playbooks_dir() -> Path:
    """Resolve playbooks root from BROWSER_MCP_PLAYBOOKS_DIR or a package-local default."""
    raw = os.environ.get("BROWSER_MCP_PLAYBOOKS_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        return p
    return (Path(__file__).resolve().parent / "playbooks").resolve()


def host_slug(host_or_url: str) -> str:
    """Normalize a URL or hostname to a safe playbook filename stem."""
    s = (host_or_url or "").strip()
    if not s:
        raise PlaybookError("host is required")
    if "://" in s:
        host = urlparse(s).hostname or ""
    else:
        host = s.split("/")[0]
    host = host.removeprefix("www.").strip().lower()
    slug = re.sub(r"[^a-z0-9.-]", "_", host)
    slug = slug.strip("._")
    if not slug or slug in (".", "..") or ".." in slug:
        raise PlaybookError(f"invalid host: {host_or_url!r}")
    return slug


def _assert_under_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PlaybookError("playbook path escapes playbooks directory") from exc
    return resolved


def playbook_path(host_or_url: str) -> Path:
    """Return playbooks/<slug>.md, guaranteed under playbooks_dir()."""
    slug = host_slug(host_or_url)
    root = playbooks_dir()
    return _assert_under_root(root / f"{slug}.md", root)


def list_playbook_names(host_or_url: str | None = None) -> list[str]:
    """List playbook filenames (*.md), optionally filtered by host prefix."""
    root = playbooks_dir()
    if not root.is_dir():
        return []
    if host_or_url:
        prefix = host_slug(host_or_url)
        return sorted(p.name for p in root.glob(f"{prefix}*.md") if p.is_file())
    return sorted(p.name for p in root.glob("*.md") if p.is_file())


def read_playbook(host_or_url: str) -> str:
    path = playbook_path(host_or_url)
    if not path.is_file():
        raise PlaybookError(f"no playbook for {host_or_url!r}")
    return path.read_text(encoding="utf-8")


def write_playbook(host_or_url: str, content: str, *, append: bool = False) -> dict[str, str | bool]:
    root = playbooks_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = playbook_path(host_or_url)
    text = content if content is not None else ""
    if append and path.is_file():
        existing = path.read_text(encoding="utf-8").rstrip()
        text = f"{existing}\n\n---\n\n{text}" if existing else text
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(path), "host": host_slug(host_or_url)}
