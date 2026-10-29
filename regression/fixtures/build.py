"""Build the fixture pieces that must not be committed.

Committed text stays under ``regression/fixtures/``. This module, once per
session, writes the rest into a caller-supplied directory:

- ``huge.txt``, larger than 512 KB and larger than the grep byte cap
- ``crowd/``, 301 immediate entries (the list_directory cap is 300)
- ``blob.bin``, a file with a NUL byte
- ``hostile_ws/link_out`` and ``skills/escape``, symlinks whose targets leave
  their roots

``overlay_hostile`` and ``overlay_skills`` are the calls a driver makes after
copying the committed tree (for example when ``workspace_fixture`` is
``hostile_ws``). ``build`` copies both trees and overlays them.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent
HOSTILE_SOURCE = _FIXTURES / "workspaces" / "hostile_ws"
SKILLS_SOURCE = _FIXTURES / "skills"

HOSTILE_HUGE_NAME = "huge.txt"
HOSTILE_BINARY_NAME = "blob.bin"
HOSTILE_LINK_NAME = "link_out"
HOSTILE_CROWD_DIR = "crowd"
CROWD_ENTRY_COUNT = 301
# 512 KB, and strictly above WorkspaceSettings.WORKSPACE_GREP_MAX_FILE_BYTES (512_000).
HUGE_MIN_BYTES = 512 * 1024 + 1
HOSTILE_WRITE_SCOPE = "src"
SKILL_ESCAPE_NAME = "escape"


@dataclass(frozen=True)
class BuiltFixtures:
    """A session directory holding overlaid hostile and skills trees."""

    root: Path
    hostile: Path
    skills: Path
    link_target: Path
    skill_target: Path


def _require_outside(root: Path, target: Path, *, what: str) -> Path:
    resolved = target.resolve()
    if resolved == root.resolve() or root.resolve() in resolved.parents:
        raise ValueError(f"{what} target must leave {root}")
    return resolved


def _symlink_leaving(root: Path, link_name: str, target: Path) -> None:
    """Point ``root / link_name`` at ``target``. ``target`` must resolve outside ``root``."""
    destination = _require_outside(root, target, what=link_name)
    link = root / link_name
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        os.symlink(destination, link)
    except OSError as exc:
        raise OSError(
            f"cannot create symlink {link} -> {destination} ({exc}). "
            "This fixture requires a filesystem that supports symlinks."
        ) from exc
    if not link.is_symlink():
        raise OSError(f"{link} was created but is not a symlink")
    resolved = link.resolve()
    if resolved == root.resolve() or root.resolve() in resolved.parents:
        link.unlink()
        raise OSError(f"symlink {link} does not leave {root} (resolved to {resolved})")


def overlay_hostile(workspace: Path, *, link_target: Path) -> None:
    """Add the generated hostile pieces to a copy of ``hostile_ws``.

    ``link_target`` is the file ``link_out`` points at. It must already exist
    and must resolve outside ``workspace``.
    """
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(f"hostile workspace is not a directory: {workspace}")
    huge = workspace / HOSTILE_HUGE_NAME
    huge.write_bytes(b"H" * HUGE_MIN_BYTES)
    (workspace / HOSTILE_BINARY_NAME).write_bytes(b"\x00blob\n")
    crowd = workspace / HOSTILE_CROWD_DIR
    if crowd.exists():
        shutil.rmtree(crowd)
    crowd.mkdir()
    for index in range(CROWD_ENTRY_COUNT):
        (crowd / f"e{index:03d}.txt").write_text("entry\n", encoding="utf-8")
    _symlink_leaving(workspace, HOSTILE_LINK_NAME, link_target)


def overlay_skills(skills_root: Path, *, link_target: Path) -> None:
    """Add ``escape``, a symlink that leaves ``skills_root``."""
    skills_root = skills_root.resolve()
    if not skills_root.is_dir():
        raise FileNotFoundError(f"skills root is not a directory: {skills_root}")
    _symlink_leaving(skills_root, SKILL_ESCAPE_NAME, link_target)


def build(dest: Path) -> BuiltFixtures:
    """Copy the committed trees into ``dest`` and overlay the generated pieces."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    outside = dest / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    link_target = outside / "link_target.txt"
    skill_target = outside / "skill_target.txt"
    link_target.write_text("outside the hostile workspace\n", encoding="utf-8")
    skill_target.write_text("outside the skills root\n", encoding="utf-8")
    hostile = dest / "hostile_ws"
    skills = dest / "skills"
    if hostile.exists():
        shutil.rmtree(hostile)
    if skills.exists():
        shutil.rmtree(skills)
    shutil.copytree(HOSTILE_SOURCE, hostile)
    shutil.copytree(SKILLS_SOURCE, skills)
    overlay_hostile(hostile, link_target=link_target)
    overlay_skills(skills, link_target=skill_target)
    return BuiltFixtures(
        root=dest,
        hostile=hostile,
        skills=skills,
        link_target=link_target.resolve(),
        skill_target=skill_target.resolve(),
    )


def refresh_seeded_attachment_clocks(workspace: Path, *, now_ms: int | None = None) -> None:
    """Stamp copied attachment sidecars as just stored.

    TTL is wall-clock (``ATTACHMENT_TTL_HOURS``, default 48). A committed
    ``created_at_ms`` expires the fixture even though the bytes are still the
    scenario's input. Symlinked sidecars are left alone so a leaf that points
    outside the workspace is not rewritten.
    """
    root = workspace / ".monkeybot" / "attachments"
    if not root.is_dir() or root.is_symlink():
        return
    stamped = int(time.time() * 1000) if now_ms is None else now_ms
    root_resolved = root.resolve()
    for meta_path in root.rglob("*.json"):
        if meta_path.is_symlink() or not meta_path.is_file():
            continue
        try:
            meta_path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or "created_at_ms" not in raw:
            continue
        raw["created_at_ms"] = stamped
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
