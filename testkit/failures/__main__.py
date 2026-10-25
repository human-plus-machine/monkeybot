"""Triage CLI: python -m testkit.failures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from testkit.failures.catalog import catalog_expect
from testkit.failures.store import (
    default_ledger_dir,
    find_failure,
    load_latest,
    load_run,
    mark,
    newest_run_file,
    open_failures,
    prune,
    run_failures,
    runs_in_group,
    top_in_repo_frame,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m testkit.failures")
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="Ledger directory (default: test-runs/ at the repo root).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    latest = sub.add_parser("latest", help="Failures from the newest run, or a whole gate group.")
    latest.add_argument("--group", default=None)

    sub.add_parser("open", help="Failures whose nodeid has not passed in a later run.")

    show = sub.add_parser("show", help="Full record for a fingerprint or run_id:seq.")
    show.add_argument("key")

    prompt = sub.add_parser("prompt", help="Print a fix brief for one fingerprint.")
    prompt.add_argument("fingerprint")

    marked = sub.add_parser("mark", help="Record a triage status in triage.json.")
    marked.add_argument("fingerprint")
    marked.add_argument(
        "--status",
        required=True,
        choices=("investigating", "fixed", "wontfix", "quarantined"),
    )
    marked.add_argument("--note", required=True)
    marked.add_argument("--link", default=None)

    exported = sub.add_parser("export", help="Print a triage report. Does not write a file.")
    exported.add_argument("--format", required=True, choices=("md", "json"), dest="fmt")

    sub.add_parser("prune", help="Apply retention now.")

    args = parser.parse_args(argv)
    directory = args.ledger_dir if args.ledger_dir is not None else default_ledger_dir()
    command = str(args.command)
    if command == "latest":
        sys.stdout.write(_format_latest(directory, args.group))
        return 0
    if command == "open":
        sys.stdout.write(_format_open(directory))
        return 0
    if command == "show":
        record = find_failure(directory, args.key)
        if record is None:
            sys.stderr.write(f"no failure matching {args.key}\n")
            return 1
        sys.stdout.write(json.dumps(record, indent=2) + "\n")
        return 0
    if command == "prompt":
        record = find_failure(directory, args.fingerprint)
        if record is None:
            sys.stderr.write(f"no failure matching {args.fingerprint}\n")
            return 1
        sys.stdout.write(render_prompt(record))
        return 0
    if command == "mark":
        try:
            entry = mark(
                directory,
                args.fingerprint,
                status=args.status,
                note=args.note,
                link=args.link,
            )
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        sys.stdout.write(json.dumps(entry) + "\n")
        return 0
    if command == "export":
        sys.stdout.write(render_export(directory, args.fmt))
        return 0
    if command == "prune":
        removed = prune(directory)
        sys.stdout.write(f"pruned {len(removed)}\n")
        return 0
    sys.stderr.write(f"unknown command {command}\n")
    return 2


def render_prompt(record: dict[str, Any]) -> str:
    """Fix brief, including the catalog Expect text for the case id."""
    path = record.get("path") or ""
    frame = top_in_repo_frame(str(record.get("traceback") or ""))
    related: list[str] = []
    if path:
        related.append(str(path))
    frame_path = frame.rsplit(":", 1)[0] if frame else ""
    if frame_path and frame_path not in related:
        related.append(frame_path)
    lines = [
        "Fix this recorded failure. Fix the code under test, not the assertion, "
        "unless the catalog Expect is wrong.",
        "",
        f"Fingerprint: {record.get('fingerprint')}",
        f"Case: {record.get('case')}",
    ]
    expect = _catalog_expect_line(record)
    if expect is not None:
        lines.append(f"Expect: {expect}")
    lines.extend(
        [
            f"Tier: {record.get('tier')}",
            f"Area: {record.get('area')}",
            f"Node: {record.get('nodeid')}",
            f"Phase: {record.get('phase')}",
            f"Outcome: {record.get('outcome')}",
            f"Error: {record.get('exc_type')}: {record.get('message')}",
            f"Repro: {record.get('repro')}",
            "",
            "Traceback:",
            str(record.get("traceback") or ""),
            "",
            "Related files:",
        ]
    )
    if related:
        lines.extend(f"- {item}" for item in related)
    else:
        lines.append("-")
    lines.append("")
    return "\n".join(lines)


def _catalog_expect_line(record: dict[str, Any]) -> str | None:
    case = record.get("case")
    live = catalog_expect(str(case)) if case else None
    if live:
        return live
    stored = record.get("catalog_expect")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return None


def render_export(directory: Path, fmt: str) -> str:
    rows = open_failures(directory)
    payload = [
        {
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "count": row["count"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "case": row["case"],
            "tier": row["tier"],
            "nodeid": row["nodeid"],
            "exc_type": row["exc_type"],
            "message": row["message"],
            "note": row["note"],
            "link": row["link"],
        }
        for row in rows
    ]
    if fmt == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = ["# Failure ledger", ""]
    if not payload:
        lines.append("No open failures.")
        lines.append("")
        return "\n".join(lines)
    for row in payload:
        lines.append(
            f"- `{row['fingerprint']}` {row['status']} count={row['count']} {row['exc_type']}"
        )
        lines.append(f"  - {row['nodeid']}")
        lines.append(f"  - {row['message']}")
    lines.append("")
    return "\n".join(lines)


def _format_latest(directory: Path, group: str | None) -> str:
    if group:
        files = runs_in_group(directory, group)
    else:
        newest = newest_run_file(directory)
        files = [newest] if newest is not None else []
    if not files:
        return "no runs\n"
    chunks: list[str] = []
    pointer = load_latest(directory)
    for path in files:
        records = load_run(path)
        failures = run_failures(records)
        header = next((record for record in records if record.get("kind") == "run_start"), {})
        footer = next(
            (record for record in reversed(records) if record.get("kind") == "run_end"), {}
        )
        run_id = header.get("run_id") or path.name[: -len(".jsonl")]
        complete = footer.get("complete")
        chunks.append(f"run {run_id} complete={complete} failures={len(failures)}")
        if pointer is not None and pointer.get("run_id") == run_id:
            chunks.append(f"path {pointer.get('path')}")
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in failures:
            tier = str(record.get("tier") or "-")
            area = str(record.get("area") or "-")
            grouped.setdefault((tier, area), []).append(record)
        for tier, area in sorted(grouped):
            chunks.append(f"[{tier}/{area}]")
            for record in grouped[(tier, area)]:
                case = record.get("case") or "-"
                chunks.append(
                    f"  {record.get('seq')}  {case}  {record.get('nodeid')}  "
                    f"{record.get('exc_type')}: {record.get('message')}"
                )
    chunks.append("")
    return "\n".join(chunks)


def _format_open(directory: Path) -> str:
    rows = open_failures(directory)
    if not rows:
        return "no open failures\n"
    lines = []
    for row in rows:
        lines.append(
            f"{row['fingerprint']}  status={row['status']}  count={row['count']}  "
            f"first={row['first_seen']}  last={row['last_seen']}  {row['nodeid']}"
        )
        lines.append(f"  {row['exc_type']}: {row['message']}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
