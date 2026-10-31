"""Before/after snapshots of workspace writes, for review and undo.

``write_file``, ``replace_in_file``, and ``apply_patch`` overwrite the file
before the client hears about it, and their results do not include the
previous body. This recorder reads the pre-image, lets the tool run, then
stores both sides (content-addressed, size-capped) under
``.monkeybot/changes/{session}/`` so the app can show a diff and restore.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService

logger = logging.getLogger(__name__)

BLOB_CAP_BYTES = 2 * 1024 * 1024
WORKSPACE_CAP_BYTES = 200 * 1024 * 1024
CONTENT_TEXT_CAP = 200_000
_SESSION_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_LEAF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

FileAction = Literal["create", "modify", "delete", "move"]


class ChangeRevertError(Exception):
    """Undo refused. ``code`` is ``conflict`` or ``too_large``; ``paths`` names the files."""

    def __init__(self, code: str, paths: list[str]) -> None:
        self.code = code
        self.paths = paths
        super().__init__(f"{code}: {', '.join(paths)}")


@dataclass(frozen=True)
class Preimage:
    """One path captured before a write tool runs."""

    path: str
    before_sha: str | None
    before: bytes | None
    too_large: bool
    is_text: bool
    from_path: str | None = None
    action_hint: FileAction | None = None


@dataclass(frozen=True)
class FileChangeRecord:
    """One recorded file mutation. Bodies live in blobs, not on this object."""

    change_id: str
    request_id: str
    call_id: str
    path: str
    action: FileAction
    before_sha: str | None
    after_sha: str | None
    additions: int
    deletions: int
    is_text: bool
    too_large: bool
    ts: str
    from_path: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "change_id": self.change_id,
            "request_id": self.request_id,
            "call_id": self.call_id,
            "path": self.path,
            "action": self.action,
            "before_sha": self.before_sha,
            "after_sha": self.after_sha,
            "additions": self.additions,
            "deletions": self.deletions,
            "is_text": self.is_text,
            "too_large": self.too_large,
            "ts": self.ts,
        }
        if self.from_path:
            payload["from_path"] = self.from_path
        return payload


def _lock_for(root: Path) -> threading.Lock:
    key = str(root)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _safe_session(session_id: str) -> str:
    cleaned = session_id.strip()
    if (
        cleaned in {".", ".."}
        or not cleaned
        or len(cleaned) > 128
        or any(ch not in _SESSION_OK for ch in cleaned)
    ):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return cleaned


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _fingerprint(path: Path) -> tuple[str | None, bytes | None, bool, bool]:
    """Return ``(sha, bytes or None, is_text, too_large)``. Absent file is all empty."""
    if not path.is_file():
        return None, None, True, False
    size = path.stat().st_size
    if size > BLOB_CAP_BYTES:
        return _hash_file(path), None, False, True
    data = path.read_bytes()
    return _sha256(data), data, _is_text(data), False


def _line_stats(before: bytes | None, after: bytes | None) -> tuple[int, int]:
    old = (before or b"").decode("utf-8", errors="replace").splitlines()
    new = (after or b"").decode("utf-8", errors="replace").splitlines()
    additions = 0
    deletions = 0
    for line in difflib.unified_diff(old, new, n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _canon(workspace: WorkspaceFileService, rel: str) -> tuple[str, Path] | None:
    try:
        fp = workspace.require_writable_path(rel)
    except WorkspaceError:
        return None
    return workspace.repo_rel(fp), fp


def capture_preimages(
    workspace: WorkspaceFileService,
    tool: str,
    args: dict[str, Any],
) -> list[Preimage]:
    """Read every file a write tool is about to touch. Failures yield no snapshot."""
    try:
        return _capture_preimages(workspace, tool, args)
    except Exception:
        logger.warning("change snapshot failed for %s", tool, exc_info=True)
        return []


def _capture_preimages(
    workspace: WorkspaceFileService,
    tool: str,
    args: dict[str, Any],
) -> list[Preimage]:
    if tool in ("write_file", "replace_in_file"):
        raw = args.get("path") or args.get("file_path") or args.get("file")
        if not isinstance(raw, str) or not raw.strip():
            return []
        located = _canon(workspace, raw)
        if located is None:
            return []
        rel, fp = located
        sha, data, is_text, too_large = _fingerprint(fp)
        hint: FileAction = "create" if sha is None else "modify"
        return [
            Preimage(
                path=rel,
                before_sha=sha,
                before=data,
                too_large=too_large,
                is_text=is_text,
                action_hint=hint,
            )
        ]
    if tool != "apply_patch":
        return []
    patch_text = args.get("patch_text") or args.get("patch") or args.get("text") or ""
    if not isinstance(patch_text, str) or not patch_text.strip():
        return []
    from monkeybot.core.tools.patch import (  # noqa: PLC0415
        AddHunk,
        DeleteHunk,
        PatchError,
        UpdateHunk,
        parse_patch,
    )

    try:
        hunks = parse_patch(patch_text)
    except PatchError:
        return []
    found: list[Preimage] = []
    for hunk in hunks:
        if isinstance(hunk, AddHunk):
            located = _canon(workspace, hunk.path)
            if located is None:
                continue
            rel, fp = located
            sha, data, is_text, too_large = _fingerprint(fp)
            found.append(
                Preimage(
                    path=rel,
                    before_sha=sha,
                    before=data,
                    too_large=too_large,
                    is_text=is_text,
                    action_hint="create",
                )
            )
        elif isinstance(hunk, DeleteHunk):
            located = _canon(workspace, hunk.path)
            if located is None:
                continue
            rel, fp = located
            sha, data, is_text, too_large = _fingerprint(fp)
            found.append(
                Preimage(
                    path=rel,
                    before_sha=sha,
                    before=data,
                    too_large=too_large,
                    is_text=is_text,
                    action_hint="delete",
                )
            )
        elif isinstance(hunk, UpdateHunk):
            if hunk.move_path:
                source = _canon(workspace, hunk.path)
                dest = _canon(workspace, hunk.move_path)
                if source is None or dest is None:
                    continue
                source_rel, source_fp = source
                dest_rel, _dest_fp = dest
                sha, data, is_text, too_large = _fingerprint(source_fp)
                found.append(
                    Preimage(
                        path=dest_rel,
                        before_sha=sha,
                        before=data,
                        too_large=too_large,
                        is_text=is_text,
                        from_path=source_rel,
                        action_hint="move",
                    )
                )
            else:
                located = _canon(workspace, hunk.path)
                if located is None:
                    continue
                rel, fp = located
                sha, data, is_text, too_large = _fingerprint(fp)
                found.append(
                    Preimage(
                        path=rel,
                        before_sha=sha,
                        before=data,
                        too_large=too_large,
                        is_text=is_text,
                        action_hint="modify",
                    )
                )
    return found


class ChangeStore:
    """Blob store + append-only log for one workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = Path(workspace_root).resolve()

    def _base(self) -> Path:
        return self.root / ".monkeybot" / "changes"

    def _session_dir(self, session_id: str) -> Path:
        return self._base() / _safe_session(session_id)

    def _log_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "log.jsonl"

    def _blob_path(self, session_id: str, sha: str) -> Path:
        return self._session_dir(session_id) / "blobs" / sha

    def _reverted_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "reverted.json"

    def _notes_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "pending_notes.jsonl"

    def commit(
        self,
        session_id: str,
        preimages: list[Preimage],
        *,
        request_id: str,
        call_id: str,
    ) -> tuple[FileChangeRecord, ...]:
        if not preimages:
            return ()
        try:
            _safe_session(session_id)
        except ValueError:
            logger.warning("skipping change record for unsafe session id")
            return ()
        records: list[FileChangeRecord] = []
        with _lock_for(self.root):
            for pre in preimages:
                located = _resolve_existing(self.root, pre.path)
                after_sha, after, after_text, after_large = _fingerprint(located)
                too_large = pre.too_large or after_large
                is_text = pre.is_text and after_text and not too_large
                if pre.action_hint == "delete" or (
                    after_sha is None and pre.before_sha is not None
                ):
                    action: FileAction = "delete"
                elif pre.action_hint == "move":
                    action = "move"
                elif pre.action_hint == "create" or pre.before_sha is None:
                    action = "create"
                else:
                    action = pre.action_hint or "modify"
                if is_text:
                    additions, deletions = _line_stats(pre.before, after)
                else:
                    additions, deletions = 0, 0
                if not too_large:
                    if pre.before is not None and pre.before_sha is not None:
                        self._write_blob(session_id, pre.before_sha, pre.before)
                    if after is not None and after_sha is not None:
                        self._write_blob(session_id, after_sha, after)
                record = FileChangeRecord(
                    change_id=uuid.uuid4().hex,
                    request_id=request_id,
                    call_id=call_id,
                    path=pre.path,
                    action=action,
                    before_sha=pre.before_sha,
                    after_sha=after_sha,
                    additions=additions,
                    deletions=deletions,
                    is_text=is_text,
                    too_large=too_large,
                    ts=datetime.now(UTC).isoformat(),
                    from_path=pre.from_path,
                )
                self._append(session_id, record)
                records.append(record)
            self._enforce_cap(session_id)
        return tuple(records)

    def list_changes(self, session_id: str) -> list[dict[str, object]]:
        try:
            _safe_session(session_id)
        except ValueError:
            return []
        with _lock_for(self.root):
            entries: list[dict[str, object]] = []
            for name in self._session_names(session_id):
                reverted = self._reverted_ids(name)
                for entry in self._read_log(name):
                    change_id = entry.get("change_id")
                    entry["reverted"] = isinstance(change_id, str) and change_id in reverted
                    entries.append(entry)
        return entries

    def content(self, session_id: str, change_id: str) -> dict[str, object] | None:
        entry = self._find(session_id, change_id)
        if entry is None:
            return None
        too_large = bool(entry.get("too_large"))
        is_text = bool(entry.get("is_text"))
        payload: dict[str, object] = {
            "change_id": change_id,
            "path": entry.get("path") or "",
            "action": entry.get("action") or "",
            "is_text": is_text,
            "too_large": too_large,
            "before": None,
            "after": None,
        }
        if too_large or not is_text:
            return payload
        payload["before"] = self._read_text_blob(session_id, entry.get("before_sha"))
        payload["after"] = self._read_text_blob(session_id, entry.get("after_sha"))
        return payload

    def revert(
        self,
        session_id: str,
        *,
        change_ids: list[str] | None = None,
        request_id: str | None = None,
        force: bool = False,
    ) -> list[str]:
        """Restore each path to the earliest pre-image in the selection.

        Raises :class:`ChangeRevertError` when the file no longer matches the
        recorded result (unless ``force``) or a snapshot was too large to keep.
        """
        with _lock_for(self.root):
            entries = self._read_log(session_id)
            already = self._reverted_ids(session_id)
            selected = _select(
                entries,
                change_ids=change_ids,
                request_id=request_id,
                reverted=already,
            )
            selected = _include_later(entries, selected, already)
            if not selected:
                return []
            too_large = [str(item["path"]) for item in selected if item.get("too_large")]
            if too_large:
                raise ChangeRevertError("too_large", too_large)
            grouped = _chain_groups(selected)
            conflicts: list[str] = []
            if not force:
                for path, group in grouped.items():
                    if not _matches_expected(self.root, path, group[-1]):
                        conflicts.append(path)
            if conflicts:
                raise ChangeRevertError("conflict", conflicts)
            restored: list[str] = []
            reverted_ids = self._reverted_ids(session_id)
            for path, group in grouped.items():
                self._restore(session_id, path, group[0])
                restored.append(path)
                for item in group:
                    change_id = item.get("change_id")
                    if isinstance(change_id, str):
                        reverted_ids.add(change_id)
            self._write_reverted(session_id, reverted_ids)
            if restored:
                names = ", ".join(restored)
                self._enqueue_note(session_id, f"User undid your changes to {names}.")
            return restored

    def drain_notes(self, session_id: str) -> list[str]:
        try:
            path = self._notes_path(session_id)
        except ValueError:
            return []
        with _lock_for(self.root):
            if not path.is_file():
                return []
            lines: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    parsed = line
                if isinstance(parsed, str) and parsed.strip():
                    lines.append(parsed)
            path.unlink()
            return lines

    def delete_session(self, session_id: str) -> None:
        try:
            _safe_session(session_id)
        except ValueError:
            return
        with _lock_for(self.root):
            base = self._base()
            if not base.is_dir():
                return
            prefix = f"subagent:{session_id}:"
            for child in list(base.iterdir()):
                if child.is_dir() and (child.name == session_id or child.name.startswith(prefix)):
                    shutil.rmtree(child, ignore_errors=True)

    def _write_blob(self, session_id: str, sha: str, data: bytes) -> None:
        dest = self._blob_path(session_id, sha)
        if dest.is_file():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)

    def _append(self, session_id: str, record: FileChangeRecord) -> None:
        path = self._log_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")

    def _read_log(self, session_id: str) -> list[dict[str, object]]:
        path = self._log_path(session_id)
        if not path.is_file():
            return []
        entries: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def _find(self, session_id: str, change_id: str) -> dict[str, object] | None:
        for entry in self._read_log(session_id):
            if entry.get("change_id") == change_id:
                return entry
        return None

    def _session_names(self, session_id: str) -> list[str]:
        names = [session_id]
        base = self._base()
        if not base.is_dir():
            return names
        prefix = f"subagent:{session_id}:"
        names.extend(
            sorted(
                path.name
                for path in base.iterdir()
                if path.is_dir() and path.name.startswith(prefix)
            )
        )
        return names

    def _trash_dir(self, session_id: str, change_id: object) -> Path:
        raw = change_id if isinstance(change_id, str) else ""
        leaf = raw if raw not in {".", ".."} and _LEAF_RE.fullmatch(raw) else "invalid"
        return self._session_dir(session_id) / "trash" / leaf

    def _read_text_blob(self, session_id: str, sha: object) -> str | None:
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            return None
        path = self._blob_path(session_id, sha)
        if not path.is_file():
            return None
        text = path.read_bytes().decode("utf-8", errors="replace")
        if len(text) > CONTENT_TEXT_CAP:
            return text[:CONTENT_TEXT_CAP]
        return text

    def _reverted_ids(self, session_id: str) -> set[str]:
        path = self._reverted_path(session_id)
        if not path.is_file():
            return set()
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return set()
        if not isinstance(parsed, list):
            return set()
        return {item for item in parsed if isinstance(item, str)}

    def _write_reverted(self, session_id: str, ids: set[str]) -> None:
        path = self._reverted_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids)), encoding="utf-8")

    def _enqueue_note(self, session_id: str, note: str) -> None:
        path = self._notes_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(note, ensure_ascii=False) + "\n")

    def _restore(self, session_id: str, path: str, earliest: dict[str, object]) -> None:
        current = _within(self.root, path)
        if current is None:
            return
        action = earliest.get("action")
        before_sha = earliest.get("before_sha")
        from_path = earliest.get("from_path")
        target = current
        if action == "move" and isinstance(from_path, str):
            source = _within(self.root, from_path)
            if source is None:
                return
            target = source
        if action == "create" or not isinstance(before_sha, str) or not before_sha:
            if current.is_file():
                trash = self._trash_dir(session_id, earliest.get("change_id"))
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current), str(trash / current.name))
            return
        if _SHA_RE.fullmatch(before_sha) is None:
            return
        blob = self._blob_path(session_id, before_sha)
        if not blob.is_file():
            raise ChangeRevertError("too_large", [path])
        data = blob.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".change_tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        if action == "move" and current.is_file() and current != target:
            trash = self._trash_dir(session_id, earliest.get("change_id"))
            trash.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(trash / current.name))

    def _enforce_cap(self, keep_session: str) -> None:
        base = self._base()
        if not base.is_dir():
            return
        while _tree_size(base) > WORKSPACE_CAP_BYTES:
            sessions = [p for p in base.iterdir() if p.is_dir() and p.name != "blobs"]
            others = [p for p in sessions if p.name != keep_session]
            if not others:
                return
            others.sort(key=lambda p: p.stat().st_mtime)
            shutil.rmtree(others[0], ignore_errors=True)


def _resolve_existing(root: Path, rel: str) -> Path:
    return root / rel


def _within(root: Path, rel: str) -> Path | None:
    """``rel`` resolved under ``root``, or None when it leaves the workspace."""
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")):
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


def _matches_expected(root: Path, path: str, latest: dict[str, object]) -> bool:
    expected = latest.get("after_sha")
    fp = _within(root, path)
    if fp is None:
        return False
    if not isinstance(expected, str) or not expected:
        return not fp.is_file()
    if not fp.is_file():
        return False
    size = fp.stat().st_size
    current = _hash_file(fp) if size > BLOB_CAP_BYTES else _sha256(fp.read_bytes())
    return current == expected


def _select(
    entries: list[dict[str, object]],
    *,
    change_ids: list[str] | None,
    request_id: str | None,
    reverted: set[str],
) -> list[dict[str, object]]:
    wanted = set(change_ids or [])
    selected: list[dict[str, object]] = []
    for entry in entries:
        change_id = entry.get("change_id")
        if isinstance(change_id, str) and change_id in reverted:
            continue
        if wanted and change_id in wanted:
            selected.append(entry)
            continue
        if not wanted and request_id and entry.get("request_id") == request_id:
            selected.append(entry)
    return selected


def _include_later(
    entries: list[dict[str, object]],
    selected: list[dict[str, object]],
    reverted: set[str],
) -> list[dict[str, object]]:
    """Keep later edits of a selected path in the undo, so they cannot reapply it."""
    if not selected:
        return []
    selected_ids = {
        change_id for entry in selected if isinstance(change_id := entry.get("change_id"), str)
    }
    armed: set[str] = set()
    chosen: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in entries:
        change_id = entry.get("change_id")
        if isinstance(change_id, str) and change_id in reverted:
            continue
        raw_path = entry.get("path")
        path = raw_path if isinstance(raw_path, str) else ""
        raw_from = entry.get("from_path")
        from_path = raw_from if entry.get("action") == "move" and isinstance(raw_from, str) else ""
        hit = isinstance(change_id, str) and change_id in selected_ids
        follows = bool(path and path in armed) or bool(from_path and from_path in armed)
        if not hit and not follows:
            continue
        if isinstance(change_id, str):
            if change_id in seen:
                continue
            seen.add(change_id)
        chosen.append(entry)
        if path:
            armed.add(path)
        if from_path:
            armed.add(from_path)
    return chosen


def _chain_groups(entries: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Group a selection by where each file ended up. A move continues its source."""
    chains: dict[str, list[dict[str, object]]] = {}
    at: dict[str, str] = {}
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        from_path = entry.get("from_path")
        if entry.get("action") == "move" and isinstance(from_path, str) and from_path:
            key = at.get(from_path, from_path)
            chain = chains.pop(key, [])
            chain.append(entry)
            chains[path] = chain
            at[from_path] = path
            at[path] = path
            continue
        key = at.get(path, path)
        chains.setdefault(key, []).append(entry)
        at[path] = key
    return chains


def _tree_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


class UndoNoteHook:
    """Injects queued undo notes into the next turn so the model hears about them."""

    def register(self, manager: object) -> None:
        from monkeybot.core.hooks import HookEvent, HookManager  # noqa: PLC0415

        if not isinstance(manager, HookManager):
            return
        manager.register(HookEvent.PRE_TURN, self.on_pre_turn)

    async def on_pre_turn(self, payload: object) -> None:
        from monkeybot.core.hooks import HookPayload  # noqa: PLC0415

        if not isinstance(payload, HookPayload):
            return
        root = payload.ctx.workspace_root
        if root is None:
            return
        notes = ChangeStore(Path(root)).drain_notes(payload.ctx.session_id)
        if not notes:
            return
        block = "\n".join(notes)
        payload.inject_text = (
            f"{payload.inject_text.rstrip()}\n\n{block}" if payload.inject_text else block
        )


def commit_file_changes(
    workspace: WorkspaceFileService,
    preimages: list[Preimage],
    *,
    session_id: str,
    request_id: str,
    call_id: str,
) -> tuple[FileChangeRecord, ...]:
    if not preimages:
        return ()
    try:
        return ChangeStore(workspace.repo_root).commit(
            session_id,
            preimages,
            request_id=request_id,
            call_id=call_id,
        )
    except Exception:
        logger.warning("change record failed", exc_info=True)
        return ()
