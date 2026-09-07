"""Durable, cross-agent "Always allow" store for `run_command` binaries and
read-only folder access, plus the in-session ("Allow once") variants.

Distinct from `computer/approvals.py`, which is per-agent (`monkeybot_config/
approvals.json`) and only ever covers `computer_*` tools. The user's own
"Always allow ffmpeg" click is meant to cover every agent on this machine, not
just the one that happened to ask first — so this store lives at the
Monkeybot *home* root (or, for a headless/CLI agent with no home concept,
degrades to a per-agent file; see `core/layout.py`).

Same on-disk contract as `computer/approvals.py`: JSON, mode 0600, atomic
`os.replace`, and cross-process exclusive-create locking on a sibling
`.lock` file (NOT `fcntl.flock` — the paired Electron-side reader/writer,
`electron/main/grants.ts`, has no `flock` binding). Both sides must keep
implementing the identical create-if-absent + stale-reclaim protocol against
the identical path or the lock protects nothing.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

_LOCK_SUFFIX = ".lock"
_LOCK_STALE_S = 5.0
_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.025


@dataclass(frozen=True)
class CommandGrant:
    command: str
    created_at: str


@dataclass(frozen=True)
class PathGrant:
    path: str
    mode: str  # "read" today; reserved for future "write"
    created_at: str


@dataclass(frozen=True)
class Grants:
    commands: tuple[CommandGrant, ...] = ()
    paths: tuple[PathGrant, ...] = ()


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + _LOCK_SUFFIX)


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process mutual exclusion around `path` — see module docstring for
    why this is exclusive-create + stale-reclaim rather than `flock`."""
    lock_path = _lock_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                continue
            if age > _LOCK_STALE_S:
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for the grants lock: {lock_path}") from None
            time.sleep(_LOCK_POLL_S)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def load_grants(path: Path) -> Grants:
    if not path.exists():
        return Grants()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Grants()
    if not isinstance(data, dict):
        return Grants()

    commands: list[CommandGrant] = []
    for item in data.get("commands", []) if isinstance(data.get("commands"), list) else []:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        commands.append(
            CommandGrant(command=command.strip(), created_at=str(item.get("created_at", "")))
        )

    paths: list[PathGrant] = []
    for item in data.get("paths", []) if isinstance(data.get("paths"), list) else []:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        mode = item.get("mode", "read")
        if mode not in ("read",):
            mode = "read"
        paths.append(
            PathGrant(path=raw_path.strip(), mode=mode, created_at=str(item.get("created_at", "")))
        )

    return Grants(commands=tuple(commands), paths=tuple(paths))


def _write_atomic(path: Path, grants: Grants) -> None:
    payload = {
        "version": 1,
        "commands": [{"command": c.command, "created_at": c.created_at} for c in grants.commands],
        "paths": [
            {"path": p.path, "mode": p.mode, "created_at": p.created_at} for p in grants.paths
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".grants-", suffix=".tmp")
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


def add_command_grant(path: Path, *, command: str, created_at: str) -> None:
    with _file_lock(path):
        grants = load_grants(path)
        commands = [c for c in grants.commands if c.command != command]
        commands.append(CommandGrant(command=command, created_at=created_at))
        _write_atomic(path, Grants(commands=tuple(commands), paths=grants.paths))


def remove_command_grant(path: Path, *, command: str) -> bool:
    with _file_lock(path):
        grants = load_grants(path)
        remaining = tuple(c for c in grants.commands if c.command != command)
        if len(remaining) == len(grants.commands):
            return False
        _write_atomic(path, Grants(commands=remaining, paths=grants.paths))
        return True


def add_path_grant(path: Path, *, folder: str, mode: str, created_at: str) -> None:
    with _file_lock(path):
        grants = load_grants(path)
        paths = [p for p in grants.paths if p.path != folder]
        paths.append(PathGrant(path=folder, mode=mode, created_at=created_at))
        _write_atomic(path, Grants(commands=grants.commands, paths=tuple(paths)))


def remove_path_grant(path: Path, *, folder: str) -> bool:
    with _file_lock(path):
        grants = load_grants(path)
        remaining = tuple(p for p in grants.paths if p.path != folder)
        if len(remaining) == len(grants.paths):
            return False
        _write_atomic(path, Grants(commands=grants.commands, paths=remaining))
        return True


# Tools whose "always allow" click writes a folder read grant rather than a
# command grant. The single source of truth for "which tools does a folder
# grant cover" — path_grant_inspector.py and core_tool_executor.py both
# import this rather than keeping their own copy.
PATH_GRANT_TOOLS: frozenset[str] = frozenset({"read_file", "load_file", "glob", "grep"})


def build_grants_persist_hook(grants_path: Path) -> Callable[[str, str], bool]:
    """Build the ``persist`` callback for ``permission.remember_always_approval``,
    wired onto ``TurnContext.grants_persist``.

    Dispatches purely on ``tool`` name (the callback signature carries no
    ``grant_kind``): ``run_command`` writes a command grant, the read-path
    tools write a folder grant, anything else is a no-op (``True``, changing
    nothing) — same contract as ``computer/permissions.py::build_persist_hook``,
    which this mirrors and is independent from.
    """

    def _persist(tool: str, resource: str) -> bool:
        created_at = datetime.datetime.now(datetime.UTC).isoformat()
        if tool == "run_command":
            add_command_grant(grants_path, command=resource, created_at=created_at)
            return True
        if tool in PATH_GRANT_TOOLS:
            add_path_grant(grants_path, folder=resource, mode="read", created_at=created_at)
            return True
        return True

    return _persist


class GrantStoreCache:
    """Mtime-cached reader so a grant written mid-session (by this process or
    the Electron Settings UI) takes effect on the *next* tool call without a
    gateway restart, without re-reading the file on every single check().

    Mirrors `computer/permissions.py::ComputerAwarePermissionInspector`'s
    `(mtime_ns, size, inode)` staleness check.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stamp: tuple[int, int, int] | None = None
        self._grants = Grants()

    def _stat_stamp(self) -> tuple[int, int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def get(self) -> Grants:
        stamp = self._stat_stamp()
        if stamp != self._stamp:
            self._grants = load_grants(self._path)
            self._stamp = stamp
        return self._grants

    def command_names(self) -> frozenset[str]:
        return frozenset(c.command for c in self.get().commands)

    def path_grant_for(self, folder: str) -> PathGrant | None:
        for p in self.get().paths:
            if p.path == folder:
                return p
        return None
