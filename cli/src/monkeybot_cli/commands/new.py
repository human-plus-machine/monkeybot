"""monkeybot new — scaffold a bot workspace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from monkeybot.scaffold import run_new


def run_new_command(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    if not dest.is_dir():
        print(f"error: --dest is not a directory: {dest}", file=sys.stderr)
        return 2

    provider = args.provider
    model = args.model
    if not args.yes:
        if provider is None:
            try:
                provider = input("Model provider [gemini]: ").strip() or "gemini"
            except EOFError:
                provider = "gemini"
        if model is None:
            try:
                model = input("Model name [gemini-3-flash]: ").strip() or "gemini-3-flash"
            except EOFError:
                model = "gemini-3-flash"

    report = run_new(dest=dest, force=args.force, provider=provider, model=model)
    print(f"MonkeyBot scaffold under {dest}:")
    print("\n".join(report))
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("new", help="Scaffold monkeybot_config/ and workspace dirs")
    p.add_argument("--dest", type=Path, default=Path.cwd(), help="Root directory for the bot")
    p.add_argument("--provider", help="model.provider value for monkeybot.yaml")
    p.add_argument("--model", help="model.name value for monkeybot.yaml")
    p.add_argument("--force", action="store_true", help="Overwrite existing scaffold files")
    p.add_argument("--yes", "-y", action="store_true", help="Skip interactive prompts")
    p.set_defaults(func=run_new_command)
