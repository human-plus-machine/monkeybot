"""monkeybot trace — list and digest session transcripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from monkeybot.core.layout import resolve_workspace_root
from monkeybot.core.persistence.transcript import resolve_session_artifact_dir
from monkeybot.core.persistence.transcript_reader import (
    TRANSCRIPT_FILENAME,
    build_digest,
    digest_to_json,
    iter_session_dirs,
    record_by_seq,
    render_digest_markdown,
    scan_session_signals,
)

from monkeybot_cli.config_resolve import resolve_agent_root, resolve_config


def _workspace(args: argparse.Namespace) -> Path:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    agent_root = resolve_agent_root(cwd=cwd, config_path=config_path)
    return resolve_workspace_root(agent_root=agent_root, config_path=config_path)


def _transcripts_root(workspace: Path) -> Path:
    return workspace / ".monkeybot" / "transcripts"


def _resolve_target(raw: str | None, workspace: Path) -> Path | None:
    """Session directory from an id, a directory, or a transcript.ndjson path."""
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_file() and candidate.name == TRANSCRIPT_FILENAME:
            return candidate.parent
        if candidate.is_dir() and (candidate / TRANSCRIPT_FILENAME).is_file():
            return candidate
        session_dir = resolve_session_artifact_dir(workspace, raw)
        if (session_dir / TRANSCRIPT_FILENAME).is_file():
            return session_dir
        return None
    sessions = iter_session_dirs(_transcripts_root(workspace))
    return sessions[0] if sessions else None


def _print_list(workspace: Path, *, as_json: bool) -> int:
    sessions = iter_session_dirs(_transcripts_root(workspace))
    rows = []
    for session_dir in sessions:
        signals = scan_session_signals(session_dir)
        rows.append(
            {
                "dir": session_dir.name,
                "path": str(session_dir),
                "score": signals.score(),
                "errors": sum(signals.tool_errors.values()),
                "interventions": sum(signals.interventions.values()),
                "steers": signals.steers,
                "verdicts": signals.verdicts,
                "empty": signals.empty_assistant,
                "summaries": signals.summaries,
                "slow": len(signals.slow_tools),
                "repeated": len(signals.repeated_calls),
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"No transcripts under {_transcripts_root(workspace)}")
        return 0
    for row in rows:
        print(
            f"{row['dir']}  score={row['score']}  errors={row['errors']}  "
            f"interventions={row['interventions']}  steers={row['steers']}  "
            f"verdicts={row['verdicts']}  empty={row['empty']}  "
            f"summaries={row['summaries']}  slow={row['slow']}  repeated={row['repeated']}"
        )
    return 0


def _read_failure(exc: OSError) -> int:
    print(f"Transcript read failed: {exc}", file=sys.stderr)
    return 1


def run_trace_list(args: argparse.Namespace) -> int:
    try:
        return _print_list(_workspace(args), as_json=bool(args.json))
    except OSError as exc:
        return _read_failure(exc)


def run_trace_digest(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    session_dir = _resolve_target(args.target, workspace)
    if session_dir is None:
        target = args.target or "(latest)"
        print(f"No transcript found for {target}", file=sys.stderr)
        return 1
    try:
        seqs = args.seq or []
        if seqs:
            missing = False
            for seq in seqs:
                record = record_by_seq(session_dir, seq)
                if record is None:
                    print(f"No record with seq {seq} in {session_dir}", file=sys.stderr)
                    missing = True
                    continue
                print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
            return 1 if missing else 0
        digest = build_digest(session_dir, full=bool(args.full))
    except OSError as exc:
        return _read_failure(exc)
    if args.json:
        print(json.dumps(digest_to_json(digest), ensure_ascii=False, indent=2, default=str))
        return 0
    print(render_digest_markdown(digest, max_chars=args.max_chars))
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    trace = subparsers.add_parser(
        "trace",
        help="List and digest session transcripts for debugging and retros",
    )
    sub = trace.add_subparsers(dest="trace_command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", help="Agent project root")
    common.add_argument("--config", help="Path to monkeybot.yaml")

    listing = sub.add_parser(
        "list",
        parents=[common],
        help="List captured sessions, newest first",
    )
    listing.add_argument("--json", action="store_true", help="JSON output")
    listing.set_defaults(func=run_trace_list)

    digest = sub.add_parser(
        "digest",
        parents=[common],
        help="Condensed timeline of one session",
    )
    digest.add_argument(
        "target",
        nargs="?",
        help="Session id, session directory, or transcript.ndjson path (default: newest)",
    )
    digest.add_argument("--json", action="store_true", help="JSON output")
    digest.add_argument("--full", action="store_true", help="Do not clip excerpts")
    digest.add_argument(
        "--max-chars",
        type=int,
        default=0,
        help="Cap markdown length (0 = no cap)",
    )
    digest.add_argument(
        "--seq",
        type=int,
        action="append",
        help="Print one resolved record instead of the digest (repeatable)",
    )
    digest.set_defaults(func=run_trace_digest)
