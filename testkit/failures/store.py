"""Read and prune failure-ledger run files. Stdlib only."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_RUN_FILE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-fA-F]{7,}-\d+\.jsonl$")
_RUN_DIR = re.compile(r"^\d{8}T\d{6}Z$")
_TEMP_PATH = re.compile(r"(?:/private)?/(?:var/folders|tmp)/\S+")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_LITERAL = re.compile(r"\b0x[0-9a-fA-F]+\b")
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_DIGITS = re.compile(r"\d+")
_FRAME = re.compile(r'File "([^"]+)", line \d+, in ([^\n]+)')
_KEEP_DEFAULT = 200
_DAYS_DEFAULT = 30
_TRIAGE_STATUSES = frozenset({"investigating", "fixed", "wontfix", "quarantined"})


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_ledger_dir() -> Path:
    return repo_root() / "test-runs"


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root()).as_posix()
    except ValueError:
        return str(resolved)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            records.append(value)
    return records


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_stamp_of(run_id: str) -> str:
    """UTC start prefix of a run id, used as the folder name."""
    return run_id.split("-", 1)[0]


def run_folder(directory: Path, run_id: str) -> Path:
    return directory / run_stamp_of(run_id)


def resolve_run_file(directory: Path, run_id: str) -> Path:
    """Path of a run file. New runs live in ``<stamp>/<run id>.jsonl``.

    A file written by an older ledger, directly under ``directory``, is
    returned when it is the one that exists.
    """
    nested = run_folder(directory, run_id) / f"{run_id}.jsonl"
    if nested.is_file():
        return nested
    flat = directory / f"{run_id}.jsonl"
    if flat.is_file():
        return flat
    return nested


def list_run_files(directory: Path) -> list[Path]:
    """Run files, oldest first.

    A run's jsonl and outcomes gzip live in ``<stamp>/``. Files left at the
    top level by older ledgers are included. ``index.jsonl``, ``LATEST.json``,
    ``triage.json``, and other folders (such as ``phase0a/``) are not runs.
    """
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in directory.iterdir():
        if path.is_file() and _RUN_FILE.match(path.name):
            files.append(path)
            continue
        if not path.is_dir() or not _RUN_DIR.match(path.name):
            continue
        for child in path.iterdir():
            if child.is_file() and _RUN_FILE.match(child.name):
                files.append(child)
    files.sort(key=lambda path: path.name)
    return files


def outcomes_path(run_file: Path) -> Path:
    return run_file.with_name(run_file.name[: -len(".jsonl")] + ".outcomes.tsv.gz")


def write_outcomes(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for nodeid, outcome in rows:
            safe = nodeid.replace("\t", " ").replace("\n", " ")
            handle.write(f"{safe}\t{outcome}\n")


def read_outcomes(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    rows: list[tuple[str, str]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            nodeid, outcome = stripped.split("\t", 1)
            rows.append((nodeid, outcome))
    return rows


def load_latest(directory: Path) -> dict[str, Any] | None:
    path = directory / "LATEST.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        return value
    return None


def write_latest(directory: Path, payload: dict[str, Any]) -> None:
    write_json(directory / "LATEST.json", payload)


def append_index(directory: Path, run_end: dict[str, Any], run_file: Path) -> None:
    line = dict(run_end)
    line["path"] = display_path(run_file)
    append_jsonl(directory / "index.jsonl", line)


def _run_started(name: str) -> datetime | None:
    stamp = name.split("-", 1)[0]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def retention_limits() -> tuple[int, int]:
    keep = int(os.environ.get("MONKEYBOT_TEST_LEDGER_KEEP", str(_KEEP_DEFAULT)))
    days = int(os.environ.get("MONKEYBOT_TEST_LEDGER_DAYS", str(_DAYS_DEFAULT)))
    return keep, days


def prune(directory: Path, *, keep: int | None = None, days: int | None = None) -> list[str]:
    """Delete a run only when it is outside the newest ``keep`` and older than ``days``."""
    if keep is None or days is None:
        env_keep, env_days = retention_limits()
        if keep is None:
            keep = env_keep
        if days is None:
            days = env_days
    files = list_run_files(directory)
    protected = set(files[-keep:]) if keep > 0 else set()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed: list[str] = []
    for path in files:
        if path in protected:
            continue
        started = _run_started(path.name)
        if started is None or started >= cutoff:
            continue
        path.unlink(missing_ok=True)
        outcomes_path(path).unlink(missing_ok=True)
        parent = path.parent
        if parent != directory and _RUN_DIR.match(parent.name):
            with contextlib.suppress(OSError):
                parent.rmdir()
        removed.append(path.name)
    return removed


def mark_incomplete(directory: Path) -> list[str]:
    """Append ``run_end`` with ``complete: false`` to every run file that lacks one."""
    marked: list[str] = []
    now = iso(utc_now())
    for path in list_run_files(directory):
        records = read_jsonl(path)
        if not records or any(record.get("kind") == "run_end" for record in records):
            continue
        run_id = next(
            (record.get("run_id") for record in records if record.get("run_id")),
            path.name[: -len(".jsonl")],
        )
        footer: dict[str, Any] = {
            "kind": "run_end",
            "run_id": run_id,
            "finished_at": now,
            "exit_status": None,
            "complete": False,
            "counts": {},
        }
        append_jsonl(path, footer)
        append_index(directory, footer, path)
        latest = load_latest(directory)
        if latest is not None and latest.get("run_id") == run_id:
            latest["complete"] = False
            write_latest(directory, latest)
        marked.append(str(run_id))
    return marked


def normalize_message(message: str) -> str:
    """Drop hex ids, digit runs, and temp paths so the same bug shares a fingerprint."""
    text = _TEMP_PATH.sub("<tmp>", message)
    text = _UUID.sub("<hex>", text)
    text = _HEX_LITERAL.sub("<hex>", text)
    text = _HEX_RUN.sub("<hex>", text)
    return _DIGITS.sub("<n>", text)


def top_in_repo_frame(traceback: str, root: Path | None = None) -> str:
    """Deepest traceback frame under the repo, as ``path:function``."""
    repo = root or repo_root()
    prefix = str(repo)
    chosen = ""
    for path, func in _FRAME.findall(traceback):
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            resolved = path
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            relative = Path(resolved).relative_to(repo).as_posix()
            chosen = f"{relative}:{func.strip()}"
    return chosen


def fingerprint(nodeid: str, exc_type: str | None, message: str, frame: str) -> str:
    raw = "|".join((nodeid, exc_type or "", normalize_message(message), frame))
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def area_of(case: str | None) -> str | None:
    if not case:
        return None
    parts = case.split("-")
    if len(parts) >= 3 and parts[0].startswith("T") and parts[0][1:].isdigit():
        return parts[1]
    return None


def tier_from_nodeid(nodeid: str) -> str | None:
    """Goal 6: the tier is the path, except ``regression/selftest`` which is t1."""
    path = nodeid.split("::", 1)[0].replace("\\", "/")
    if path == "regression/selftest" or path.startswith("regression/selftest/"):
        return "t1"
    for tier in ("t2", "t3", "t4", "t5"):
        prefix = f"regression/{tier}"
        if path == prefix or path.startswith(prefix + "/"):
            return tier
    if path == "tests" or path.startswith("tests/"):
        return "t1"
    return None


def run_failures(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record.get("kind") == "failure"]


def load_run(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)


def newest_run_file(directory: Path) -> Path | None:
    files = list_run_files(directory)
    if not files:
        return None
    latest = load_latest(directory)
    if latest is not None:
        run_id = latest.get("run_id")
        if isinstance(run_id, str):
            candidate = resolve_run_file(directory, run_id)
            if candidate.is_file():
                return candidate
    return files[-1]


def runs_in_group(directory: Path, group: str) -> list[Path]:
    matched: list[Path] = []
    for path in list_run_files(directory):
        for record in read_jsonl(path):
            if record.get("kind") == "run_start" and record.get("group") == group:
                matched.append(path)
                break
    return matched


def triage_path(directory: Path) -> Path:
    return directory / "triage.json"


def load_triage(directory: Path) -> dict[str, Any]:
    path = triage_path(directory)
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        return value
    return {}


def mark(
    directory: Path,
    fingerprint_id: str,
    *,
    status: str,
    note: str,
    link: str | None = None,
) -> dict[str, Any]:
    if status not in _TRIAGE_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    triage = load_triage(directory)
    entry: dict[str, Any] = {
        "status": status,
        "note": note,
        "link": link,
        "updated_at": iso(utc_now()),
    }
    triage[fingerprint_id] = entry
    write_json(triage_path(directory), triage)
    return entry


def find_failure(directory: Path, key: str) -> dict[str, Any] | None:
    if ":" in key:
        run_id, seq_text = key.rsplit(":", 1)
        try:
            seq = int(seq_text)
        except ValueError:
            return None
        path = resolve_run_file(directory, run_id)
        for record in read_jsonl(path):
            if record.get("kind") == "failure" and record.get("seq") == seq:
                return record
        return None
    found: dict[str, Any] | None = None
    for path in list_run_files(directory):
        for record in read_jsonl(path):
            if record.get("kind") == "failure" and record.get("fingerprint") == key:
                found = record
    return found


def open_failures(directory: Path) -> list[dict[str, Any]]:
    """Fingerprints that have not passed in a later run, oldest first."""
    triage = load_triage(directory)
    open_by_fp: dict[str, dict[str, Any]] = {}
    for path in list_run_files(directory):
        records = read_jsonl(path)
        run_id = path.name[: -len(".jsonl")]
        seen_at = ""
        for record in records:
            if record.get("kind") == "run_start":
                seen_at = str(record.get("started_at") or "")
                run_id = str(record.get("run_id") or run_id)
        for record in run_failures(records):
            fp = str(record.get("fingerprint") or "")
            if not fp:
                continue
            current = open_by_fp.get(fp)
            if current is None:
                open_by_fp[fp] = {
                    "fingerprint": fp,
                    "nodeid": record.get("nodeid"),
                    "case": record.get("case"),
                    "tier": record.get("tier"),
                    "area": record.get("area"),
                    "exc_type": record.get("exc_type"),
                    "message": record.get("message"),
                    "count": 1,
                    "first_seen": seen_at,
                    "last_seen": seen_at,
                    "last_run_id": run_id,
                    "closed": False,
                    "record": record,
                }
            else:
                current["count"] = int(current["count"]) + 1
                current["last_seen"] = seen_at
                current["last_run_id"] = run_id
                current["closed"] = False
                current["record"] = record
                current["message"] = record.get("message")
        for nodeid, outcome in read_outcomes(outcomes_path(path)):
            if outcome != "passed":
                continue
            for entry in open_by_fp.values():
                if entry["closed"]:
                    continue
                if entry["nodeid"] == nodeid and entry["last_run_id"] != run_id:
                    entry["closed"] = True
    rows = [entry for entry in open_by_fp.values() if not entry["closed"]]
    for entry in rows:
        info = triage.get(entry["fingerprint"])
        if isinstance(info, dict) and info.get("status"):
            entry["status"] = info["status"]
            entry["note"] = info.get("note")
            entry["link"] = info.get("link")
        else:
            entry["status"] = "untriaged"
            entry["note"] = None
            entry["link"] = None
    return rows
