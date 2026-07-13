"""monkeybot run — launch the SSE gateway as a subprocess."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from monkeybot_cli.config_resolve import load_agent_dotenv, resolve_agent_root, resolve_config
from monkeybot_cli.runtime_python import gateway_argv, resolve_runtime_python


def run_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    env = os.environ.copy()
    if config_path is not None:
        env["MONKEYBOT_CONFIG"] = str(config_path)
    if args.port:
        env["PORT"] = str(args.port)
    # --cwd intentionally pins the subprocess and runtime directory. Without
    # it, an explicit config determines the agent root.
    agent_root = resolve_agent_root(cwd=cwd, config_path=config_path)
    runtime = resolve_runtime_python(agent_root)
    cmd = gateway_argv(runtime)
    try:
        proc = subprocess.run(cmd, env=env, cwd=agent_root)
        return proc.returncode
    except KeyboardInterrupt:
        return 130


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("run", help="Start the monkeybot SSE gateway")
    p.add_argument("--config", help="Path to monkeybot.yaml (sets MONKEYBOT_CONFIG)")
    p.add_argument("--port", type=int, help="Listen port (sets PORT)")
    p.add_argument("--cwd", help="Working directory for the gateway process")
    p.set_defaults(func=run_run)
