"""Pytest plugin and LedgerWriter. Stdlib only, so cli/ and browser-mcp can load it."""

from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from testkit.failures.catalog import catalog_expect
from testkit.failures.store import (
    append_index,
    append_jsonl,
    area_of,
    default_ledger_dir,
    display_path,
    fingerprint,
    iso,
    mark_incomplete,
    outcomes_path,
    prune,
    repo_root,
    run_folder,
    run_stamp,
    tier_from_nodeid,
    top_in_repo_frame,
    utc_now,
    write_latest,
    write_outcomes,
)
from testkit.scrub import scrub

_TB_CAP = 16 * 1024
_CAPTURE_CAP = 4 * 1024
_MESSAGE_CAP = 16 * 1024
_TIERS = ("t1", "t2", "t3", "t4", "t5")
_EXC_NAME = re.compile(r"^[A-Za-z_][\w.]*$")

_state: _Session | None = None
_stack: list[_Session | None] = []
_banners: dict[int, str] = {}
_warned = False
_warn_lock = threading.Lock()


class _Session:
    def __init__(self, writer: LedgerWriter, session: Any) -> None:
        self.writer = writer
        self.session = session
        self.phases: dict[str, dict[str, str]] = {}
        self.seen_collect: set[str] = set()

    def items_by_nodeid(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for item in getattr(self.session, "items", ()) or ():
            nodeid = getattr(item, "nodeid", None)
            if isinstance(nodeid, str):
                found[nodeid] = item
        return found


class LedgerWriter:
    """Append-only run file. T4 and T5 runners call this directly."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_ledger_dir()
        self.run_id = ""
        self.path: Path | None = None
        self.failures: list[dict[str, Any]] = []
        self._seq = 0
        self._finished = False
        self._lock = threading.Lock()

    def start(
        self,
        *,
        argv: list[str] | None = None,
        markexpr: str | None = None,
        workers: int | None = None,
        live: dict[str, Any] | None = None,
        trigger: str | None = None,
        group: str | None = None,
    ) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        prune(self.directory)
        mark_incomplete(self.directory)
        started = utc_now()
        info = git_info(repo_root())
        sha = info.get("sha")
        sha7 = sha[:7] if isinstance(sha, str) and sha else "0000000"
        pid = os.getpid()
        nonce = 0
        while True:
            suffix = str(pid) if nonce == 0 else f"{pid}{nonce}"
            self.run_id = f"{run_stamp(started)}-{sha7}-{suffix}"
            candidate = run_folder(self.directory, self.run_id) / f"{self.run_id}.jsonl"
            flat = self.directory / f"{self.run_id}.jsonl"
            if not candidate.exists() and not flat.exists():
                self.path = candidate
                break
            nonce += 1
        if group is None:
            group = os.environ.get("MONKEYBOT_TEST_RUN_GROUP") or None
        if trigger is None:
            trigger = os.environ.get("MONKEYBOT_TEST_TRIGGER") or "manual"
        header: dict[str, Any] = {
            "kind": "run_start",
            "schema_version": 1,
            "run_id": self.run_id,
            "group": group,
            "started_at": iso(started),
            "git": info,
            "trigger": trigger,
            "argv": argv if argv is not None else current_argv(),
            "markexpr": markexpr or None,
            "python": platform.python_version(),
            "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
            "workers": workers,
            "live": live,
        }
        append_jsonl(self.path, header)
        return self.run_id

    def record_failure(self, **fields: Any) -> dict[str, Any]:
        if self.path is None:
            raise RuntimeError("LedgerWriter.start() was not called")
        message = _head(scrub(str(fields.get("message") or "")), _MESSAGE_CAP)
        traceback = _tail(scrub(str(fields.get("traceback") or "")), _TB_CAP)
        captured = _scrub_captured(fields.get("captured"))
        exc_type = fields.get("exc_type")
        exc_name = str(exc_type) if exc_type else None
        nodeid = str(fields.get("nodeid") or "")
        frame = top_in_repo_frame(traceback)
        fp = fields.get("fingerprint") or fingerprint(nodeid, exc_name, message, frame)
        case = fields.get("case")
        case_id = str(case) if case else None
        with self._lock:
            self._seq += 1
            record: dict[str, Any] = {
                "kind": "failure",
                "run_id": self.run_id,
                "seq": self._seq,
                "severity": fields.get("severity") or "fail",
                "tier": fields.get("tier"),
                "case": case_id,
                "area": fields.get("area") if fields.get("area") is not None else area_of(case_id),
                "nodeid": nodeid,
                "path": fields.get("path"),
                "line": fields.get("line"),
                "phase": fields.get("phase") or "call",
                "outcome": fields.get("outcome") or "failed",
                "duration_s": fields.get("duration_s"),
                "worker": fields.get("worker"),
                "exc_type": exc_name,
                "message": message,
                "traceback": traceback,
                "captured": captured,
                "scenario": fields.get("scenario"),
                "artifacts": list(fields.get("artifacts") or []),
                "repro": fields.get("repro") or repro_command(nodeid),
                "fingerprint": fp,
                "catalog_expect": catalog_expect(case_id),
            }
            if "live" in fields:
                record["live"] = fields["live"]
            if "trials" in fields:
                record["trials"] = fields["trials"]
            append_jsonl(self.path, record)
            self.failures.append(record)
            return record

    def finish(
        self,
        *,
        exit_status: int,
        counts: dict[str, int],
        outcomes: list[tuple[str, str]],
    ) -> None:
        if self.path is None or self._finished:
            return
        self._finished = True
        finished = iso(utc_now())
        footer: dict[str, Any] = {
            "kind": "run_end",
            "run_id": self.run_id,
            "finished_at": finished,
            "exit_status": int(exit_status),
            "complete": True,
            "counts": counts,
        }
        append_jsonl(self.path, footer)
        write_outcomes(outcomes_path(self.path), outcomes)
        shown = display_path(self.path)
        write_latest(
            self.directory,
            {
                "run_id": self.run_id,
                "path": shown,
                "finished_at": finished,
                "complete": True,
                "counts": counts,
            },
        )
        append_index(self.directory, footer, self.path)

    @property
    def shown_path(self) -> str:
        if self.path is None:
            return ""
        return display_path(self.path)


def pytest_addoption(parser: Any) -> None:
    def _add() -> None:
        group = parser.getgroup("monkeybot", "monkeybot failure ledger")
        group.addoption(
            "--ledger-dir",
            action="store",
            default=None,
            help="Directory for failure-ledger run files (default: test-runs/).",
        )
        group.addoption(
            "--no-ledger",
            action="store_true",
            default=False,
            help="Do not write a failure ledger for this run.",
        )

    _guard(_add)


def pytest_sessionstart(session: Any) -> None:
    _guard(_begin, session)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    _guard(_end, session, exitstatus)


def pytest_runtest_logreport(report: Any) -> None:
    _guard(_on_logreport, report)


def pytest_collectreport(report: Any) -> None:
    _guard(_on_collect, report)


def pytest_internalerror(excrepr: Any, excinfo: Any) -> None:
    _guard(_on_internal, excrepr, excinfo)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    def _print() -> None:
        text = _banners.pop(id(config), None)
        if not text:
            return
        write = getattr(terminalreporter, "write", None)
        if write is None:
            sys.stdout.write(text + "\n")
            return
        write(text + "\n")

    _guard(_print)


def _begin(session: Any) -> None:
    global _state, _warned
    _stack.append(_state)
    _state = None
    _warned = False
    config = session.config
    if not _enabled(config):
        return
    directory = _ledger_directory(config)
    writer = LedgerWriter(directory)
    writer.start(
        argv=current_argv(),
        markexpr=_markexpr(config),
        workers=_workers(config),
    )
    _state = _Session(writer, session)


def _end(session: Any, exitstatus: int) -> None:
    global _state
    try:
        current = _state
        if current is None or current.session is not session:
            return
        counts = _counts(current)
        outcomes = _outcomes(current)
        current.writer.finish(exit_status=int(exitstatus), counts=counts, outcomes=outcomes)
        if current.writer.failures:
            _banners[id(session.config)] = format_banner(
                current.writer.failures, current.writer.shown_path
            )
    finally:
        _state = _stack.pop() if _stack else None


def _on_logreport(report: Any) -> None:
    current = _state
    if current is None or not _is_test_report(report):
        return
    nodeid = str(getattr(report, "nodeid", ""))
    phase = str(getattr(report, "when", "call"))
    kind = _classify(report)
    current.phases.setdefault(nodeid, {})[phase] = kind
    if getattr(report, "outcome", None) != "failed":
        return
    _record_report(current, report, phase)


def _on_collect(report: Any) -> None:
    current = _state
    if current is None or getattr(report, "outcome", None) != "failed":
        return
    if _is_test_report(report):
        return
    nodeid = str(getattr(report, "nodeid", ""))
    # xdist re-collects on every worker, so the controller sees one report per worker.
    if nodeid in current.seen_collect:
        return
    current.seen_collect.add(nodeid)
    current.phases.setdefault(nodeid, {})["collect"] = "error"
    _record_report(current, report, "collect")


def _on_internal(excrepr: Any, excinfo: Any) -> None:
    current = _state
    if current is None:
        return
    message = str(getattr(excinfo, "value", excinfo))
    typename = getattr(excinfo, "typename", None)
    current.writer.record_failure(
        nodeid="",
        path=None,
        phase="internal",
        outcome="error",
        exc_type=str(typename) if typename else "InternalError",
        message=message,
        traceback=str(excrepr),
        repro="uv run pytest -p no:xdist",
    )


def _record_report(current: _Session, report: Any, phase: str) -> None:
    nodeid = str(getattr(report, "nodeid", ""))
    item = current.items_by_nodeid().get(nodeid)
    exc_type, message, traceback = _exception_parts(report)
    location = getattr(report, "location", None)
    line: int | None = None
    path = nodeid.split("::", 1)[0] or None
    if isinstance(location, tuple) and len(location) >= 2 and isinstance(location[1], int):
        line = location[1] + 1 if phase != "collect" else location[1]
        if location[0]:
            path = str(location[0])
    duration = getattr(report, "duration", None)
    duration_s = round(float(duration), 6) if isinstance(duration, int | float) else None
    current.writer.record_failure(
        tier=_tier(item, nodeid),
        case=_case(item, report),
        nodeid=nodeid,
        path=path,
        line=line,
        phase=phase,
        outcome=str(getattr(report, "outcome", "failed")),
        duration_s=duration_s,
        worker=_worker(report),
        exc_type=exc_type,
        message=message,
        traceback=traceback,
        captured=_captured(report),
        repro=repro_command(nodeid) if nodeid else "uv run pytest -p no:xdist",
    )


def _enabled(config: Any) -> bool:
    if os.environ.get("MONKEYBOT_TEST_LEDGER") == "0":
        return False
    if bool(getattr(config.option, "no_ledger", False)):
        return False
    return not hasattr(config, "workerinput")


def _ledger_directory(config: Any) -> Path:
    raw = getattr(config.option, "ledger_dir", None)
    if raw:
        return Path(str(raw))
    return default_ledger_dir()


def _markexpr(config: Any) -> str | None:
    raw = getattr(config.option, "markexpr", None)
    if raw:
        return str(raw)
    return None


def _workers(config: Any) -> int | None:
    raw = getattr(config.option, "numprocesses", None)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _is_test_report(report: Any) -> bool:
    return getattr(report, "when", None) in {"setup", "call", "teardown"}


def _classify(report: Any) -> str:
    wasxfail = hasattr(report, "wasxfail")
    outcome = getattr(report, "outcome", "")
    when = getattr(report, "when", "")
    if outcome == "skipped" and wasxfail:
        return "xfailed"
    if outcome == "passed" and wasxfail:
        return "xpassed"
    if outcome == "failed" and _is_strict_xpass(report):
        return "xpassed_strict"
    if outcome == "failed" and when in {"setup", "teardown"}:
        return "error"
    if outcome == "failed":
        return "failed"
    if outcome == "skipped":
        return "skipped"
    return "passed"


def _is_strict_xpass(report: Any) -> bool:
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, str) and longrepr.startswith("[XPASS(strict)]"):
        return True
    text = getattr(report, "longreprtext", "")
    return isinstance(text, str) and "[XPASS(strict)]" in text


def _node_outcome(phases: dict[str, str]) -> str:
    if phases.get("collect") == "error" or phases.get("setup") == "error":
        return "error"
    call = phases.get("call")
    if call in {"failed", "xpassed_strict"}:
        return call
    if phases.get("teardown") == "error":
        return "error"
    if call == "xpassed":
        return "passed"
    if call:
        return call
    return "passed"


def _counts(current: _Session) -> dict[str, int]:
    counts = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed_strict": 0,
    }
    for phases in current.phases.values():
        kind = _node_outcome(phases)
        if kind == "passed" or kind == "xpassed":
            counts["passed"] += 1
        elif kind == "failed":
            counts["failed"] += 1
        elif kind == "error":
            counts["errors"] += 1
        elif kind == "skipped":
            counts["skipped"] += 1
        elif kind == "xfailed":
            counts["xfailed"] += 1
        elif kind == "xpassed_strict":
            counts["xpassed_strict"] += 1
    return counts


def _outcomes(current: _Session) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for nodeid, phases in current.phases.items():
        rows.append((nodeid, _node_outcome(phases)))
    return rows


def _tier(item: Any, nodeid: str) -> str | None:
    if item is not None:
        get_marker = getattr(item, "get_closest_marker", None)
        if get_marker is not None:
            for name in _TIERS:
                if get_marker(name) is not None:
                    return name
        keywords = getattr(item, "keywords", None)
        if keywords is not None:
            for name in _TIERS:
                if name in keywords:
                    return name
    return tier_from_nodeid(nodeid)


def _case(item: Any, report: Any) -> str | None:
    if item is not None:
        get_marker = getattr(item, "get_closest_marker", None)
        if get_marker is not None:
            marker = get_marker("case")
            args = getattr(marker, "args", None) if marker is not None else None
            if args:
                return str(args[0])
        for key, value in getattr(item, "user_properties", ()) or ():
            if key == "case" and value is not None:
                return str(value)
    for key, value in getattr(report, "user_properties", ()) or ():
        if key == "case" and value is not None:
            return str(value)
    return None


def _exception_parts(report: Any) -> tuple[str | None, str, str]:
    longrepr = getattr(report, "longrepr", None)
    text = ""
    if longrepr is not None:
        rendered = getattr(report, "longreprtext", None)
        text = rendered if isinstance(rendered, str) and rendered else str(longrepr)
    message = ""
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None and getattr(crash, "message", None):
        message = str(crash.message)
    elif isinstance(longrepr, str):
        message = longrepr
    elif isinstance(longrepr, tuple) and len(longrepr) >= 3:
        message = str(longrepr[2])
    message = message.splitlines()[0] if message else ""
    if _is_strict_xpass(report):
        fallback = text.splitlines()[0] if text else ""
        return "XPass", message or fallback, text
    exc_type = _exc_type(message, text)
    if exc_type and message.startswith(f"{exc_type}:"):
        message = message[len(exc_type) + 1 :].strip()
    if exc_type is None and (message.startswith("assert ") or "AssertionError" in text):
        exc_type = "AssertionError"
    return exc_type, message, text


def _exc_type(message: str, text: str) -> str | None:
    for source in (message, text):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("E "):
                stripped = stripped[2:].strip()
            head = stripped.split(":", 1)[0].strip()
            if _EXC_NAME.match(head) and head.split(".")[-1].endswith(
                ("Error", "Exception", "Interrupt", "Exit", "Warning")
            ):
                return head.split(".")[-1]
    return None


def _captured(report: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, attr in (("stdout", "capstdout"), ("stderr", "capstderr"), ("log", "caplog")):
        value = getattr(report, attr, "")
        found[key] = value if isinstance(value, str) else ""
    return found


def _scrub_captured(captured: Any) -> dict[str, str]:
    source = captured if isinstance(captured, dict) else {}
    cleaned: dict[str, str] = {}
    for key in ("stdout", "stderr", "log"):
        value = source.get(key, "")
        text = value if isinstance(value, str) else ""
        cleaned[key] = _tail(scrub(text), _CAPTURE_CAP)
    return cleaned


def _worker(report: Any) -> str | None:
    node = getattr(report, "node", None)
    gateway = getattr(node, "gateway", None)
    worker_id = getattr(gateway, "id", None)
    if worker_id:
        return str(worker_id)
    return None


def _head(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def repro_command(nodeid: str) -> str:
    if not nodeid:
        return "uv run pytest -p no:xdist"
    return f"uv run pytest {shlex.quote(nodeid)} -p no:xdist"


def current_argv() -> list[str]:
    args = [str(arg) for arg in sys.argv]
    if args and "pytest" in Path(args[0]).name:
        return ["pytest", *args[1:]]
    return args


def git_info(root: Path) -> dict[str, Any]:
    sha = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain")
    return {
        "sha": sha,
        "branch": branch,
        "dirty": None if status is None else bool(status.strip()),
    }


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def format_banner(failures: list[dict[str, Any]], path: str) -> str:
    lines = [
        "==================== monkeybot failure ledger ====================",
        f"{len(failures)} failures recorded to {path}",
    ]
    for record in failures[:20]:
        case = record.get("case") or "-"
        shown = record.get("path") or record.get("nodeid") or "-"
        exc_type = record.get("exc_type") or ""
        lines.append(f"  {case}  {shown}  {exc_type}")
    if len(failures) > 20:
        lines.append("  ...")
    lines.append("These failures are recorded for later triage. Agents: do not change code or")
    lines.append(
        "tests to fix them in this task. Report the run file and continue (docs/testing.md §5)."
    )
    lines.append("Inspect: uv run python -m testkit.failures latest")
    lines.append("======================================================================")
    return "\n".join(lines)


def _guard(fn: Any, *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        _warn(exc)


def _warn(exc: BaseException) -> None:
    global _warned
    with _warn_lock:
        if _warned:
            return
        _warned = True
    sys.stderr.write(f"monkeybot failure ledger warning: {type(exc).__name__}: {exc}\n")


# Re-export path helpers the tests and the CLI share.
__all__ = ["LedgerWriter", "format_banner"]
