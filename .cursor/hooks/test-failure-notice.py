#!/usr/bin/env python3
"""Tell the agent about a new failing test run. Advisory: always exit 0."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LATEST = ROOT / "test-runs" / "LATEST.json"
STATE = ROOT / ".cursor" / "hooks" / ".state" / "last_notified"

# Confirmed from a live postToolUse payload (2026-10-25): the shell command is
# tool_input.command. A top-level command is accepted if a future payload uses it.
_TEST_COMMAND = re.compile(
    r"(?:^|[^\w.])(?:pytest|regression\.t4|regression\.t5|ci-local\.sh)(?:$|[^\w.])"
    r"|(?:^|[^\w])git\s+push\b"
)


def main() -> None:
    data = json.load(sys.stdin)
    command = command_text(data)
    if not command or _TEST_COMMAND.search(command) is None:
        return
    latest = load_latest()
    if latest is None or not has_failures(latest):
        return
    run_id = str(latest.get("run_id") or "")
    if not run_id or run_id == read_state():
        return
    write_state(run_id)
    json.dump({"additional_context": notice(latest)}, sys.stdout)
    sys.stdout.write("\n")


def command_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    raw = data.get("tool_input")
    if isinstance(raw, dict):
        command = raw.get("command")
        if isinstance(command, str):
            return command
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
            return parsed["command"]
    command = data.get("command")
    return command if isinstance(command, str) else ""


def load_latest() -> dict | None:
    if not LATEST.is_file():
        return None
    value = json.loads(LATEST.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def has_failures(latest: dict) -> bool:
    counts = latest.get("counts")
    if not isinstance(counts, dict):
        return False
    return any(int(counts.get(key) or 0) > 0 for key in ("failed", "errors", "xpassed_strict"))


def read_state() -> str:
    if not STATE.is_file():
        return ""
    return STATE.read_text(encoding="utf-8").strip()


def write_state(run_id: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(run_id + "\n", encoding="utf-8")


def notice(latest: dict) -> str:
    counts = latest.get("counts") if isinstance(latest.get("counts"), dict) else {}
    path = latest.get("path") or ""
    run_id = latest.get("run_id") or ""
    return (
        "monkeybot failure ledger: a test command just finished a run that has failures. "
        f"Run file: {path} (run_id {run_id}; "
        f"failed={int(counts.get('failed') or 0)}, "
        f"errors={int(counts.get('errors') or 0)}, "
        f"xpassed_strict={int(counts.get('xpassed_strict') or 0)}). "
        "This is a finding to report, not a task. Do not edit source, tests, scenarios, "
        "goldens, cassettes, or baselines to make it pass, and do not skip, quarantine, "
        "xfail, delete, or loosen a check. List this run file and each failure's case id, "
        "path, and one-line error, then continue the original task. "
        "Inspect: uv run python -m testkit.failures latest"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write("")
    raise SystemExit(0)
