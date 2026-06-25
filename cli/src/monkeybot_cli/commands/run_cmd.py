"""monkeybot run — launch the SSE gateway as a subprocess."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from monkeybot_cli.config_resolve import load_agent_dotenv, resolve_config


def run_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    env = os.environ.copy()
    if config_path is not None:
        env["MONKEYBOT_CONFIG"] = str(config_path)
    if args.port:
        env["PORT"] = str(args.port)
    workdir = cwd
    cmd = [sys.executable, "-m", "monkeybot.gateway.main"]
    try:
        proc = subprocess.run(cmd, env=env, cwd=workdir)
        return proc.returncode
    except KeyboardInterrupt:
        return 130


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("run", help="Start the MonkeyBot SSE gateway")
    p.add_argument("--config", help="Path to monkeybot.yaml (sets MONKEYBOT_CONFIG)")
    p.add_argument("--port", type=int, help="Listen port (sets PORT)")
    p.add_argument("--cwd", help="Working directory for the gateway process")
    p.set_defaults(func=run_run)
