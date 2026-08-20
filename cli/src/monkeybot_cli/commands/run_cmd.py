"""monkeybot run — launch the SSE gateway as a subprocess."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from pathlib import Path

from monkeybot_cli.config_resolve import (
    load_agent_dotenv,
    load_config_doc,
    resolve_agent_root,
    resolve_config,
)
from monkeybot_cli.opensandbox_lifecycle import (
    ensure_opensandbox_for_agent,
    is_sandbox_enabled,
    server_url_from_config,
)
from monkeybot_cli.runtime_python import gateway_argv, prepare_runtime_python


def run_gateway_process(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
) -> int:
    """Start the gateway and forward SIGINT/SIGTERM/SIGHUP to it.

    ``subprocess.run`` does not forward those signals: this CLI shim dies and
    leaves uvicorn holding the port. Electron's quit path (and Ctrl-C) depend
    on SIGTERM reaching the grandchild.
    """
    proc = subprocess.Popen(cmd, env=env, cwd=cwd)

    def _forward(signum: int, _frame: object | None) -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except OSError:
                pass

    restored: dict[int, signal.Handlers] = {}
    forwarded = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        forwarded.append(signal.SIGHUP)
    for sig in forwarded:
        try:
            restored[int(sig)] = signal.signal(sig, _forward)
        except (ValueError, OSError):
            pass
    try:
        return int(proc.wait())
    except KeyboardInterrupt:
        _forward(int(signal.SIGINT), None)
        try:
            return int(proc.wait(timeout=8))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return 130
    finally:
        for sig, handler in restored.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


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
    if config_path is not None:
        _, cfg_doc = load_config_doc(config_path)
        if is_sandbox_enabled(cfg_doc):
            if not ensure_opensandbox_for_agent(
                agent_root,
                server_url=server_url_from_config(cfg_doc),
                # Fail fast: Mac app health-checks the gateway immediately.
                docker_wait_secs=2.0,
            ):
                print(
                    "Continuing without a healthy OpenSandbox — run_command may fail.",
                    flush=True,
                )
    runtime = prepare_runtime_python(agent_root, config_path)
    cmd = gateway_argv(runtime)
    try:
        return run_gateway_process(cmd, env=env, cwd=agent_root)
    except KeyboardInterrupt:
        return 130


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser("run", help="Start the monkeybot SSE gateway")
    p.add_argument("--config", help="Path to monkeybot.yaml (sets MONKEYBOT_CONFIG)")
    p.add_argument("--port", type=int, help="Listen port (sets PORT)")
    p.add_argument("--cwd", help="Working directory for the gateway process")
    p.set_defaults(func=run_run)
