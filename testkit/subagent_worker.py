"""Scripted subagent child selected by ``MONKEYBOT_SUBAGENT_SCRIPT``.

The parent writes a :class:`~monkeybot.core.subagents.subagent_proto.SubagentEnvelope`
to stdin. ``task`` is either a tape stem
(``regression/fixtures/subagent_tapes/<task>.ndjson``) or a directive
(``hang``, ``exit:<code>``). The envelope has no tape field, so the stem has
to ride in ``task``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_TAPE_STEM = re.compile(r"^[A-Za-z0-9_-]+$")
_REPO = Path(__file__).resolve().parents[1]
_TAPES = _REPO / "regression" / "fixtures" / "subagent_tapes"


def main() -> None:
    """Read one envelope and replay its tape."""
    try:
        _run()
    except SystemExit:
        raise
    except Exception as exc:
        _emit(_error_line(f"fake worker failed: {exc}"))
        raise SystemExit(1) from exc


def _run() -> None:
    try:
        decoded = json.loads(sys.stdin.read() or "")
    except json.JSONDecodeError as exc:
        _emit(_error_line(f"bad envelope: {exc}"))
        raise SystemExit(1) from exc
    if not isinstance(decoded, dict):
        _emit(_error_line("envelope root must be an object"))
        raise SystemExit(1)
    task = decoded.get("task")
    if not isinstance(task, str) or not task.strip():
        _emit(_error_line("envelope task must be a non-empty string"))
        raise SystemExit(1)
    task = task.strip()
    if task == "hang":
        _hang(_workspace())
        return
    if task.startswith("sleep:"):
        _sleep(task, _workspace())
        return
    if task.startswith("exit:"):
        raise SystemExit(_exit_code(task))

    try:
        text = _tape_path(task).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        _emit(_error_line(str(exc)))
        raise SystemExit(1) from exc

    control, events = _split_control(text.splitlines())
    workspace = _workspace()
    try:
        _apply_control(control, workspace, decoded)
    except (OSError, ValueError) as exc:
        _emit(_error_line(str(exc)))
        raise SystemExit(1) from exc

    pad = control.get("pad_event_bytes")
    if isinstance(pad, int) and not isinstance(pad, bool) and pad > 0:
        _emit(_padded_snapshot(pad))
    redact = control.get("redact_pipe") is True
    for line in events:
        _emit(_maybe_redact(line, redact))

    code = control.get("exit_code", 0)
    if isinstance(code, bool) or not isinstance(code, int):
        _emit(_error_line("exit_code must be an integer"))
        raise SystemExit(1)
    raise SystemExit(code)


def _hang(workspace: Path | None) -> None:
    """Stay alive until the parent times out or cancels the process group."""
    if workspace is not None:
        _write_pgid(workspace)
    while True:
        time.sleep(3600)


def _sleep(task: str, workspace: Path | None) -> None:
    """Sleep ``sleep:<seconds>`` then emit a short completed tape and exit 0."""
    raw = task.split(":", 1)[1]
    try:
        seconds = float(raw)
    except ValueError as exc:
        _emit(_error_line(f"bad sleep directive: {task}"))
        raise SystemExit(1) from exc
    if seconds < 0:
        _emit(_error_line(f"bad sleep directive: {task}"))
        raise SystemExit(1)
    if workspace is not None:
        _write_pgid(workspace)
    time.sleep(seconds)
    _emit('{"type": "AssistantDelta", "request_id": "child", "delta": "slept"}')
    _emit('{"type": "TurnComplete", "request_id": "child"}')
    raise SystemExit(0)


def _exit_code(task: str) -> int:
    raw = task.split(":", 1)[1]
    try:
        return int(raw)
    except ValueError as exc:
        _emit(_error_line(f"bad exit directive: {task}"))
        raise SystemExit(1) from exc


def _tape_path(task: str) -> Path:
    if _TAPE_STEM.fullmatch(task) is None:
        raise ValueError(f"tape stem must be a single path segment: {task!r}")
    path = _TAPES / f"{task}.ndjson"
    if not path.is_file():
        raise ValueError(f"tape not found: {path}")
    return path


def _split_control(lines: list[str]) -> tuple[dict[str, Any], list[str]]:
    """A leading ``{"control": true, ...}`` line is not printed."""
    control: dict[str, Any] = {}
    events: list[str] = []
    seen = False
    for line in lines:
        if not line.strip():
            continue
        if not seen:
            seen = True
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and obj.get("control") is True:
                control = obj
                continue
        events.append(line)
    return control, events


def _apply_control(
    control: dict[str, Any],
    workspace: Path | None,
    envelope: dict[str, Any],
) -> None:
    needs_workspace = any(
        key in control for key in ("write_files", "echo_envelope", "probe_concurrency")
    )
    if needs_workspace and workspace is None:
        raise ValueError("MONKEYBOT_SUBAGENT_WORKSPACE is not a directory")
    if "write_files" in control:
        assert workspace is not None
        _write_files(workspace, control["write_files"])
    if "echo_envelope" in control:
        assert workspace is not None
        _echo(workspace, envelope, control["echo_envelope"])
    if control.get("probe_concurrency") is True:
        assert workspace is not None
        hold = control.get("hold_seconds", 0.8)
        if isinstance(hold, bool) or not isinstance(hold, (int, float)):
            raise ValueError("hold_seconds must be a number")
        _probe_concurrency(workspace, float(hold))
    elif "hold_seconds" in control:
        hold = control.get("hold_seconds")
        if isinstance(hold, bool) or not isinstance(hold, (int, float)):
            raise ValueError("hold_seconds must be a number")
        if workspace is not None:
            _write_pgid(workspace)
        time.sleep(float(hold))


def _workspace() -> Path | None:
    raw = os.environ.get("MONKEYBOT_SUBAGENT_WORKSPACE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    return path


def _write_pgid(workspace: Path) -> None:
    pid = os.getpid()
    try:
        pgid = os.getpgrp()
    except OSError:
        pgid = pid
    (workspace / "subagent.pgid").write_text(f"{pid}\n{pgid}\n", encoding="utf-8")


def _write_files(workspace: Path, files: object) -> None:
    if not isinstance(files, dict):
        raise ValueError("write_files must be an object")
    root = workspace.resolve()
    for rel, text in files.items():
        if not isinstance(rel, str) or not isinstance(text, str):
            raise ValueError("write_files entries must be strings")
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"write_files path escapes the workspace: {rel}")
        dest = (root / relative).resolve()
        dest.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


def _echo(workspace: Path, envelope: dict[str, Any], name: object) -> None:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("echo_envelope must be a file name")
    payload = {
        "envelope": envelope,
        "MEMORY_STORAGE_URI": os.environ.get("MEMORY_STORAGE_URI", ""),
        "MONKEYBOT_SUBAGENT_SKILLS_PATH": os.environ.get("MONKEYBOT_SUBAGENT_SKILLS_PATH", ""),
        "MONKEYBOT_SUBAGENT_WORKSPACE": os.environ.get("MONKEYBOT_SUBAGENT_WORKSPACE", ""),
    }
    (workspace / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _probe_concurrency(workspace: Path, hold_seconds: float) -> None:
    """Record how many siblings are alive, then hold so the cap can overlap."""
    import fcntl

    lock_path = workspace / ".subagent-alive.lock"
    live = workspace / ".subagent-alive"
    live.mkdir(exist_ok=True)
    token = live / str(os.getpid())
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            token.write_text("1", encoding="utf-8")
            count = sum(1 for path in live.iterdir() if path.is_file())
            with (workspace / "concurrency_peaks.txt").open("a", encoding="utf-8") as peaks:
                peaks.write(f"{count}\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    time.sleep(hold_seconds)
    token.unlink(missing_ok=True)


def _padded_snapshot(nbytes: int) -> str:
    """One SystemPromptSnapshot line of at least ``nbytes`` bytes, no redaction."""
    prefix = '{"type":"SystemPromptSnapshot","request_id":"child","inner_turn":0,"text":"'
    suffix = '"}'
    filler = max(0, nbytes - len(prefix) - len(suffix))
    return prefix + ("A" * filler) + suffix


def _maybe_redact(line: str, enabled: bool) -> str:
    """Run snapshot lines through the real pipe redaction before printing."""
    if not enabled:
        return line
    from monkeybot.core.runtime.events import EventDecodeError, event_from_json, event_to_json
    from monkeybot.core.subagents.subagent_worker import _event_for_ndjson_pipe

    try:
        event = event_from_json(line)
    except EventDecodeError:
        return line
    redacted = _event_for_ndjson_pipe(event)
    if redacted is event:
        return line
    return event_to_json(redacted)


def _error_line(message: str) -> str:
    return json.dumps(
        {"type": "Error", "request_id": "child", "error": message},
        ensure_ascii=False,
    )


def _emit(line: str) -> None:
    sys.stdout.write(line.rstrip("\r\n") + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
