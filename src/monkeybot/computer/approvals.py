"""Durable "Always allow" store for ``computer_*`` tools.

``permissions.yaml`` is intentionally not touched by this feature: it is a
comment-heavy file the user hand-edits in the app's Advanced settings (a whole-
file textarea, saved wholesale), and a PyYAML round-trip would silently destroy
comments/formatting on the first "Always allow" click. This module owns a
separate, machine-only JSON file instead: ``monkeybot_config/approvals.json``.

Approvals are converted to :class:`~monkeybot.core.tools.permission.PermissionRule`
objects and layered *after* the built-in ask-baseline and the user's
``permissions.yaml`` when the ruleset is assembled (see ``computer/permissions.py``),
so last-match-wins semantics naturally give a remembered approval priority over
the ask default while still letting a hand-written ``deny`` in ``permissions.yaml``
win over everything.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Granularity of a durable "Always allow" grant: a specific resource, or the
#: whole tool. Re-exported as ``computer.AlwaysScope`` — this is the one
#: canonical definition, kept here since ``approvals.py`` has no dependency on
#: ``computer/__init__.py`` (avoiding the reverse would risk a cycle).
Scope = Literal["resource", "tool"]

_LOCK_SUFFIX = ".lock"


@dataclass(frozen=True)
class ApprovalRecord:
    tool: str
    resource: str
    scope: Scope
    created_at: str


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + _LOCK_SUFFIX)


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Best-effort advisory lock so concurrent readers/writers don't tear the file.

    POSIX-only (``fcntl``); no-ops elsewhere since these tools are macOS-only
    already (see ``safety.require_macos``).
    """
    lock_path = _lock_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl

        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except ImportError:
        yield


def load_approvals(path: Path) -> list[ApprovalRecord]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw_list = data.get("approvals")
    if not isinstance(raw_list, list):
        return []
    records: list[ApprovalRecord] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        resource = item.get("resource")
        scope = item.get("scope", "resource")
        created_at = item.get("created_at", "")
        if not isinstance(tool, str) or not isinstance(resource, str):
            continue
        if scope not in ("resource", "tool"):
            scope = "resource"
        records.append(
            ApprovalRecord(tool=tool, resource=resource, scope=scope, created_at=str(created_at))
        )
    return records


def _write_atomic(path: Path, records: list[ApprovalRecord]) -> None:
    payload = {
        "version": 1,
        "approvals": [
            {"tool": r.tool, "resource": r.resource, "scope": r.scope, "created_at": r.created_at}
            for r in records
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".approvals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def add_approval(path: Path, *, tool: str, resource: str, scope: Scope, created_at: str) -> None:
    with _file_lock(path):
        records = load_approvals(path)
        records = [r for r in records if not (r.tool == tool and r.resource == resource)]
        records.append(
            ApprovalRecord(tool=tool, resource=resource, scope=scope, created_at=created_at)
        )
        _write_atomic(path, records)


def remove_approval(path: Path, *, tool: str, resource: str) -> bool:
    """Delete a stored approval matching ``(tool, resource)`` exactly. Returns
    whether a record was actually removed.

    No production Python caller: revoking a rule from monkeyapp's Settings ->
    Permissions UI reads and rewrites ``approvals.json`` directly in TypeScript
    against the same file/lock format (see ``electron/main/agent-approvals.ts``),
    rather than shelling out to this module. Kept here — and exercised by
    ``tests/computer/test_approvals.py`` — as the reference implementation of
    that format and for any future in-process (e.g. CLI) caller.
    """
    with _file_lock(path):
        records = load_approvals(path)
        remaining = [r for r in records if not (r.tool == tool and r.resource == resource)]
        if len(remaining) == len(records):
            return False
        _write_atomic(path, remaining)
        return True


def to_permission_rules(records: list[ApprovalRecord]) -> list[tuple[str, str, str]]:
    """Convert records to ``(tool, pattern, effect)`` triples for the ruleset.

    ``resource`` is a literal value, not a wildcard the user typed — it must be
    escaped with :func:`glob.escape` before use as an ``fnmatch`` pattern, or a
    filename containing ``*``/``?``/``[`` would silently over-match other files.
    """
    triples: list[tuple[str, str, str]] = []
    for r in records:
        pattern = "*" if r.scope == "tool" else glob.escape(r.resource)
        triples.append((r.tool, pattern, "allow"))
    return triples
