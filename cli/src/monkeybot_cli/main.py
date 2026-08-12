"""monkeybot CLI entry point."""

from __future__ import annotations

import argparse
import sys

from monkeybot_cli.commands import chat, doctor, loop, new, refresh, run_cmd, talk, validate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monkeybot",
        description="Create, configure, validate, and chat with monkeybot agents.",
    )
    parser.add_argument("--json", action="store_true", help="JSON output (validate/doctor)")
    sub = parser.add_subparsers(dest="command", required=True)
    new.register(sub)
    refresh.register(sub)
    validate.register(sub)
    doctor.register(sub)
    run_cmd.register(sub)
    chat.register(sub)
    talk.register(sub)
    loop.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
