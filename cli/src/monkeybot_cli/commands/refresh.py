"""monkeybot refresh — additive update of packaged YAML defaults on an existing agent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from monkeybot_cli.scaffold import run_refresh


def run_refresh_command(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    if not dest.is_dir():
        print(f"error: --dest is not a directory: {dest}", file=sys.stderr)
        return 2
    try:
        report = run_refresh(dest=dest)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"monkeybot refresh under {dest}:")
    print("\n".join(report))
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "refresh",
        help=(
            "Update an existing agent's packaged YAML defaults without overwriting "
            "persona, MCP, or model settings"
        ),
    )
    p.add_argument("--dest", type=Path, default=Path.cwd(), help="Root directory for the bot")
    p.set_defaults(func=run_refresh_command)
