"""monkeybot run — launch the SSE gateway as a subprocess."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from monkeybot_cli.chat_local_shell import _kill_process_tree, _popen_kwargs_for_platform
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

_IS_WINDOWS = sys.platform == "win32"
_SHUTDOWN_TIMEOUT_SECS = 5.0
_KILL_TIMEOUT_SECS = 1.0
_Handler = Callable[[int, FrameType | None], Any] | int | None


def _cli_exit_status(rc: int | None, received: int | None) -> int:
    """Map a child ``wait()`` status to a CLI exit code.

    ``wait()`` returns a negative signal number when the child dies from a
    signal; ``SystemExit(-15)`` becomes shell status 241. Unix convention is
    ``128 + signal`` (143 for SIGTERM, 130 for SIGINT).
    """
    if received == signal.SIGINT:
        return 130
    if rc is None:
        return 1
    if rc < 0:
        return 128 + (-rc)
    return rc


def _signal_process_group(proc: subprocess.Popen[bytes], signum: int) -> None:
    if proc.poll() is not None:
        return
    if _IS_WINDOWS:
        try:
            proc.send_signal(signum)
        except OSError as exc:
            print(f"failed to signal gateway: {exc}", file=sys.stderr)
        return
    try:
        os.killpg(proc.pid, signum)
        return
    except ProcessLookupError:
        return
    except (PermissionError, OSError) as exc:
        try:
            proc.send_signal(signum)
        except OSError:
            print(f"failed to signal gateway: {exc}", file=sys.stderr)


def run_gateway_process(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    shutdown_timeout: float = _SHUTDOWN_TIMEOUT_SECS,
    kill_timeout: float = _KILL_TIMEOUT_SECS,
) -> int:
    """Start the gateway and forward stop signals to its process group.

    Electron sends SIGTERM only to this CLI shim. ``subprocess.run`` dies on
    that signal and leaves uvicorn holding the port. The child is started in a
    new session so SIGTERM/SIGINT reach the whole tree (``uv run`` plus the
    gateway grandchild), then escalate to SIGKILL if it ignores the signal.

    Interactive Ctrl-C already hits the foreground process group; forwarding
    SIGINT matters here because the child is in a new session and would
    otherwise keep running.
    """
    received: int | None = None
    proc: subprocess.Popen[bytes] | None = None

    def _forward(signum: int, _frame: object | None) -> None:
        nonlocal received
        if received is None:
            received = signum
        if proc is not None:
            _signal_process_group(proc, signum)

    restored: dict[int, _Handler] = {}
    forwarded = [signal.SIGINT, signal.SIGTERM]
    if not _IS_WINDOWS:
        forwarded.append(signal.SIGHUP)
    for sig in forwarded:
        try:
            restored[sig] = signal.signal(sig, _forward)
        except (ValueError, OSError) as exc:
            print(f"failed to install {sig.name} handler: {exc}", file=sys.stderr)
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            **_popen_kwargs_for_platform(),
        )
        if received is not None:
            _signal_process_group(proc, received)
        while True:
            timeout = shutdown_timeout if received is not None else 0.25
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if received is None:
                    continue
                print(
                    "gateway did not exit after signal; killing process tree",
                    file=sys.stderr,
                )
                _kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=kill_timeout)
                except subprocess.TimeoutExpired:
                    print("gateway still running after kill", file=sys.stderr)
                rc = proc.returncode
            return _cli_exit_status(rc, received)
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
