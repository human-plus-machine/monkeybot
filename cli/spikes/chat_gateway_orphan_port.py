"""Spike: does `monkeybot chat` leave a gateway grandchild holding the port?

The direct child is a shim, the same shape as ``uv run python``. On SIGTERM the
shim exits without signaling its child. The grandchild binds ``PORT`` and is
the process that would make the next ``monkeybot chat`` fail with
"port already in use".

This drives the real ``run_chat`` teardown:

- ``/bye`` and a normal session return go through ``run_chat``'s ``finally``
- Ctrl-C is a ``KeyboardInterrupt`` from the session, same ``finally``
- startup failure is the health-check miss path, which returns before that
  ``finally``

Exit 0 means every case released the port. Exit 1 means at least one orphan
still holds it. Exit 2 means the scenario never armed (setup error).
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from monkeybot_cli.commands.chat import run_chat
from monkeybot_cli.runtime_python import RuntimePython

CaseKind = Literal["bye", "interrupt", "startup"]
ChildSignal = Literal["die", "ignore"]

_GRANDCHILD = textwrap.dedent(
    """\
    import json, os, signal
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    if os.environ.get("SPIKE_IGNORE_SIGTERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/health":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    # Pid is recorded before accept() so a failed bind cannot look like a leak.
    Path(os.environ["SPIKE_PIDFILE"]).write_text(str(os.getpid()))
    ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
    """
)

_SHIM = textwrap.dedent(
    """\
    import os, signal, subprocess, sys

    def _exit_without_children(*_args: object) -> None:
        # A shim such as `uv run` can die on SIGTERM without killing the
        # grandchild that actually bound the port.
        os._exit(0)

    signal.signal(signal.SIGTERM, _exit_without_children)
    signal.signal(signal.SIGINT, _exit_without_children)
    subprocess.Popen([sys.executable, os.environ["SPIKE_GRANDCHILD"]])
    while True:
        signal.pause()
    """
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _listening(port: int) -> bool:
    """True when a process accepts TCP connections on ``port``.

    ``SO_REUSEADDR`` plus ``bind()`` is not a listen check on macOS: a second
    bind can succeed while another process is already accepting.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _write_agent(root: Path, port: int) -> None:
    cfg = root / "monkeybot_config"
    cfg.mkdir(parents=True)
    (cfg / "monkeybot.yaml").write_text(
        f"model:\n  provider: gemini\n  name: spike\nruntime:\n  port: {port}\n",
        encoding="utf-8",
    )


def _run_case(
    *,
    kind: CaseKind,
    child_signal: ChildSignal,
    agent: Path,
    shim: Path,
    grandchild: Path,
) -> tuple[str, str]:
    port = _free_port()
    pidfile = agent / f"{kind}-{child_signal}.pid"
    if pidfile.exists():
        pidfile.unlink()
    env = os.environ.copy()
    env["MONKEYBOT_CHAT_PLAIN"] = "1"
    env["SPIKE_GRANDCHILD"] = str(grandchild)
    env["SPIKE_PIDFILE"] = str(pidfile)
    env["SPIKE_IGNORE_SIGTERM"] = "1" if child_signal == "ignore" else "0"
    env.pop("SANDBOX_ENABLED", None)

    args = argparse.Namespace(
        cwd=str(agent),
        config=None,
        attach=False,
        url=None,
        port=port,
        model_provider=None,
        model_name=None,
        show_thinking=False,
        verbose=False,
        usage=False,
        session=None,
        resume_last=False,
        no_animations=True,
        theme="auto",
    )

    async def _session_returns(*_args: object, **_kwargs: object) -> int:
        # /bye and a normal session return both leave run_chat through finally.
        return 0

    async def _session_interrupted(*_args: object, **_kwargs: object) -> int:
        # Ctrl-C is caught around asyncio.run(_plain_chat_session).
        raise KeyboardInterrupt

    def _health_fails_once_listening(
        base: str, proc: subprocess.Popen[str], timeout_s: float = 30.0
    ) -> bool:
        del base, timeout_s
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            if _listening(port):
                return False
            time.sleep(0.05)
        raise RuntimeError(f"startup case: nothing accepted connections on {port}")

    patches = [
        patch(
            "monkeybot_cli.commands.chat.prepare_runtime_python",
            lambda *_a, **_k: RuntimePython([sys.executable, str(shim)], "spike"),
        ),
        patch("monkeybot_cli.commands.chat.is_sandbox_enabled", return_value=False),
    ]
    if kind == "startup":
        patches.append(
            patch(
                "monkeybot_cli.commands.chat._wait_for_health",
                _health_fails_once_listening,
            )
        )
    else:
        session = _session_interrupted if kind == "interrupt" else _session_returns
        patches.append(patch("monkeybot_cli.commands.chat._plain_chat_session", session))

    previous = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    started = time.monotonic()

    def _alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"{kind}/{child_signal}: run_chat hung")

    previous_alarm = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(20)
    try:
        with patches[0], patches[1]:
            if len(patches) == 3:
                with patches[2]:
                    code = run_chat(args)
            else:
                code = run_chat(args)
    except Exception as exc:
        _reap(pidfile)
        return "setup-error", f"{kind}/{child_signal}: run_chat raised {exc!r}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        os.environ.clear()
        os.environ.update(previous)
    elapsed = time.monotonic() - started
    expected = {"bye": 0, "interrupt": 130, "startup": 1}[kind]
    if not pidfile.is_file():
        return "setup-error", (
            f"{kind}/{child_signal}: grandchild never wrote a pid "
            f"(run_chat={code}, expected={expected})"
        )

    grandchild_pid = int(pidfile.read_text().strip())
    if code != expected:
        _kill_pid(grandchild_pid)
        return "setup-error", (
            f"{kind}/{child_signal}: run_chat returned {code}, expected {expected}"
        )
    # The teardown under test has already returned. A short poll only absorbs
    # SIGKILL reaping; it must not be long enough to hide a live orphan.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _pid_alive(grandchild_pid) and not _listening(port):
            break
        time.sleep(0.05)
    alive = _pid_alive(grandchild_pid)
    listening = _listening(port)
    if alive or listening:
        _kill_pid(grandchild_pid)
        return (
            "leak",
            f"{kind}/{child_signal}: LEAK port={port} pid={grandchild_pid} "
            f"alive={alive} listening={listening} run_chat={code} elapsed={elapsed:.2f}s",
        )
    return (
        "freed",
        f"{kind}/{child_signal}: FREED port={port} pid={grandchild_pid} "
        f"run_chat={code} elapsed={elapsed:.2f}s",
    )


def _reap(pidfile: Path) -> None:
    if not pidfile.is_file():
        return
    try:
        _kill_pid(int(pidfile.read_text().strip()))
    except ValueError:
        return


def _probe_uv_run() -> str:
    """Report whether `uv run python` forwards one SIGTERM to its child.

    This does not decide the chat bug. It checks the example in the report.
    """
    uv = shutil_which("uv")
    if uv is None:
        return "uv-probe: skipped (uv not on PATH)"
    probe = textwrap.dedent(
        """\
        import os, signal, time
        from pathlib import Path
        Path(os.environ["SPIKE_UV_PIDFILE"]).write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
        """
    )
    with tempfile.TemporaryDirectory(prefix="monkeybot-uv-probe-") as raw:
        root = Path(raw)
        pidfile = root / "child.pid"
        proc = subprocess.Popen(
            [uv, "run", "--no-project", "python", "-c", probe],
            env={**os.environ, "SPIKE_UV_PIDFILE": str(pidfile)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not pidfile.is_file():
                if proc.poll() is not None:
                    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    return (
                        f"uv-probe: uv exited {proc.returncode} before child ready: {err.strip()}"
                    )
                time.sleep(0.05)
            if not pidfile.is_file():
                return "uv-probe: child pid file never appeared"
            child = int(pidfile.read_text().strip())
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3)
                uv_exit = f"uv exited {proc.returncode} within 3s"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                uv_exit = "uv still running after 3s; SIGKILL'd the uv pid"
            time.sleep(0.3)
            if _pid_alive(child):
                _kill_pid(child)
                return (
                    f"uv-probe: SIGTERM to uv pid {proc.pid} left child {child} alive "
                    f"({uv_exit}; a single-pid signal does not free an ignoring grandchild)"
                )
            return f"uv-probe: SIGTERM to uv pid {proc.pid} also killed child {child} ({uv_exit})"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            if pidfile.is_file():
                try:
                    _kill_pid(int(pidfile.read_text().strip()))
                except ValueError:
                    pass


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def run_cases() -> int:
    """Run the chat teardown cases. 0 means every port was released."""
    cases: list[tuple[CaseKind, ChildSignal]] = [
        ("bye", "die"),
        ("interrupt", "die"),
        ("startup", "die"),
        ("bye", "ignore"),
    ]
    results: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="monkeybot-chat-port-spike-") as raw:
        root = Path(raw)
        agent = root / "agent"
        _write_agent(agent, port=1)
        grandchild = root / "grandchild.py"
        grandchild.write_text(_GRANDCHILD, encoding="utf-8")
        shim = root / "shim.py"
        shim.write_text(_SHIM, encoding="utf-8")
        for kind, child_signal in cases:
            status, line = _run_case(
                kind=kind,
                child_signal=child_signal,
                agent=agent,
                shim=shim,
                grandchild=grandchild,
            )
            results.append((status, line))
            print(line, flush=True)
    leaks = [line for status, line in results if status == "leak"]
    errors = [line for status, line in results if status == "setup-error"]
    if errors:
        print("SPIKE_RESULT=SETUP_ERROR", flush=True)
        return 2
    if leaks:
        print("SPIKE_RESULT=BUG_REPRODUCED", flush=True)
        return 1
    print("SPIKE_RESULT=PORT_RELEASED", flush=True)
    return 0


def main() -> int:
    print(_probe_uv_run(), flush=True)
    return run_cases()


if __name__ == "__main__":
    raise SystemExit(main())
