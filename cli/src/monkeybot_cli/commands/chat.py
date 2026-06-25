"""monkeybot chat — terminal SSE client for the gateway."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NamedTuple, TextIO

import httpx
from monkeybot.core.runtime.events import (
    ActionRequiredEvent,
    AssistantDelta,
    ContextSummarized,
    ContextSummarizing,
    Error,
    FrontendToolRequestEvent,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    event_from_json,
)

from monkeybot_cli.config_resolve import load_agent_dotenv, load_config_doc, resolve_config

DEFAULT_PORT = 8080
_SUBAGENT_HINT_MAX = 60
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


def _format_http_error(operation: str, exc: httpx.HTTPError) -> str:
    return f"{operation} failed: {exc}"


class _SpinnerLine:
    """One animated status line (thinking, tool running, summarizing)."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def clear(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    async def finish(self, line: str) -> None:
        await self.clear()
        sys.stdout.write(f"{line}\n")
        sys.stdout.flush()

    async def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            sys.stdout.write(f"\r{_DIM}  {frame} {self._prefix}{_RESET}")
            sys.stdout.flush()
            i += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.08)
            except TimeoutError:
                pass


def _collapse_hint(text: str) -> str:
    return " ".join(text.split())


def _truncate_subagent_hint(text: str) -> str:
    """Cap default subagent status-line hints; --verbose still shows full payloads."""
    collapsed = _collapse_hint(text)
    if len(collapsed) <= _SUBAGENT_HINT_MAX:
        return collapsed
    return collapsed[:_SUBAGENT_HINT_MAX] + "…"


def _tool_hint(args: dict[str, object]) -> str:
    """Summary of tool args for status lines."""
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        return _collapse_hint(" ".join(str(x) for x in argv))

    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        extra = args.get("args")
        if extra is None:
            extra = args.get("arguments")
        if isinstance(extra, list) and extra:
            line = " ".join([cmd.strip(), *[str(x) for x in extra]])
        else:
            line = cmd.strip()
        return _collapse_hint(line)

    for key in ("shell", "script", "path", "query", "url"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return _collapse_hint(val.strip())
    keys = list(args.keys())
    if len(keys) == 1:
        key = keys[0]
        val = args[key]
        if isinstance(val, (str, int, bool, float)):
            return _collapse_hint(f"{key}: {val}")
    if keys:
        return f"{len(keys)} arg{'s' if len(keys) != 1 else ''}"
    return ""


def _task_subagent_label(args: dict[str, object]) -> str:
    for key in ("subagent_type", "type", "persona"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return f"subagent:{val.strip()}"
    return "subagent"


def _task_hint(args: dict[str, object]) -> str:
    for key in ("task", "instructions", "prompt", "objective"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return _truncate_subagent_hint(val.strip())
    return ""


def _tool_display(tool: str, label: str, args: dict[str, object]) -> str:
    if tool == "task":
        hint = _task_hint(args)
        if not hint and label.strip() and label.strip() != tool:
            hint = _truncate_subagent_hint(label.strip())
        base = _task_subagent_label(args)
        return base + (f" — {hint}" if hint else "")
    hint = _tool_hint(args)
    if not hint and label.strip() and label.strip() != tool:
        hint = label.strip()
    return tool + (f" — {hint}" if hint else "")


def _tool_spinner_prefix(tool: str, label: str, args: dict[str, object]) -> str:
    if tool == "task":
        hint = _task_hint(args)
        if not hint and label.strip() and label.strip() != tool:
            hint = _truncate_subagent_hint(label.strip())
        base = "spawning " + _task_subagent_label(args)
        return base + (f" — {hint}" if hint else "")
    return _tool_display(tool, label, args)


class _TurnActivity:
    """Animated status lines for tools and context actions (playground-style)."""

    def __init__(self) -> None:
        self._line: _SpinnerLine | None = None
        self._active_display: str = ""

    async def _start(self, prefix: str) -> None:
        if self._line is not None:
            await self._line.clear()
        self._line = _SpinnerLine(prefix)
        self._line.start()

    async def tool_started(self, tool: str, label: str, args: dict[str, object]) -> None:
        self._active_display = _tool_display(tool, label, args)
        await self._start(_tool_spinner_prefix(tool, label, args))

    async def tool_finished(
        self,
        tool: str,
        *,
        error: str | None = None,
        verbose: bool = False,
        result: str = "",
    ) -> None:
        display = self._active_display or tool
        if error:
            err = " ".join(error.split())
            line = f"{_DIM}  {_RED}✗{_RESET}{_DIM} {display} — {err}{_RESET}"
        else:
            line = f"{_DIM}  {_GREEN}✓{_RESET}{_DIM} {display}{_RESET}"
        self._active_display = ""
        if self._line is not None:
            await self._line.finish(line)
            self._line = None
        else:
            sys.stdout.write(f"{line}\n")
            sys.stdout.flush()
        if verbose:
            payload = error or result or ""
            if payload:
                print(f"{_DIM}    {payload}{_RESET}")

    async def summarizing(self, tokens: int) -> None:
        await self._start(f"summarizing context ({tokens:,} tokens)")

    async def summarized(self, turns: int) -> None:
        noun = "turn" if turns == 1 else "turns"
        line = f"{_DIM}  {_GREEN}✓{_RESET}{_DIM} summarized {turns} {noun}{_RESET}"
        if self._line is not None:
            await self._line.finish(line)
            self._line = None
        else:
            sys.stdout.write(f"{line}\n")
            sys.stdout.flush()

    async def cancel(self) -> None:
        if self._line is not None:
            await self._line.clear()
            self._line = None


async def _read_line(prompt: str, interrupt: asyncio.Event) -> str | None:
    """Read a line from stdin without blocking process exit on Ctrl+C."""
    if interrupt.is_set():
        return None
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _read() -> None:
        try:
            line = input(prompt)
            loop.call_soon_threadsafe(future.set_result, line)
        except EOFError:
            loop.call_soon_threadsafe(future.set_result, "")

    threading.Thread(target=_read, daemon=True).start()
    interrupt_waiter = asyncio.create_task(interrupt.wait())
    try:
        done, pending = await asyncio.wait(
            [future, interrupt_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if interrupt_waiter in done:
            return None
        return future.result()
    except asyncio.CancelledError:
        return None


_EXIT_COMMANDS = frozenset({"/bye", "/quit", "/exit"})


def _is_exit_command(line: str) -> bool:
    return line.strip().lower() in _EXIT_COMMANDS


def _print_welcome(*, spawned_gateway: bool) -> None:
    hint = "Type /bye to exit"
    if spawned_gateway:
        hint += " (stops the gateway)"
    print(f"{_DIM}{hint}. Ctrl-C also exits.{_RESET}\n")


def _port_from_config(config_path: Path | None) -> int:
    """Read runtime.port from the resolved monkeybot.yaml (falls back to the gateway default)."""
    if config_path is None:
        return DEFAULT_PORT
    _, doc = load_config_doc(str(config_path))
    runtime = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
    try:
        return int(runtime.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _resolve_base_url(args: argparse.Namespace, config_path: Path | None) -> str:
    """Explicit --url wins; otherwise derive host:port from config."""
    if args.url:
        return args.url.rstrip("/")
    port = args.port if args.port else _port_from_config(config_path)
    return f"http://127.0.0.1:{port}"


async def _iter_sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line == "" and data_lines:
            yield "\n".join(data_lines)
            data_lines = []
    if data_lines:
        yield "\n".join(data_lines)


def _print_event(
    evt: Any,
    *,
    show_thinking: bool,
    show_usage: bool,
    request_id: str,
) -> str | None:
    """Render one event. Returns 'turn_complete', 'error', or None."""
    if show_thinking and evt.request_id == request_id:
        kind = getattr(evt, "kind", "")
        if kind in ("Thinking", "ThinkingBlockDelta"):
            text = getattr(evt, "text", "") or getattr(evt, "delta", "")
            if text:
                print(f"{_DIM}[thinking] {text}{_RESET}", flush=True)
    if isinstance(evt, Error) and evt.request_id == request_id:
        print(f"\n{_RED}Error: {evt.error}{_RESET}", flush=True)
        return "error"
    if isinstance(evt, TurnComplete) and evt.request_id == request_id:
        print()
        if show_usage:
            u = evt.usage
            print(
                f"{_DIM}[usage] in={u.input_tokens} out={u.output_tokens} "
                f"cost=${u.cost_usd:.4f} {u.duration_ms}ms{_RESET}"
            )
        return "turn_complete"
    return None


async def _handle_hitl(
    client: httpx.AsyncClient,
    base: str,
    session_id: str,
    evt: Any,
) -> None:
    if isinstance(evt, ToolConfirmationRequestEvent):
        prompt = evt.prompt or f"Approve tool {evt.tool_name}?"
        try:
            ans = input(f"{prompt} [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        approved = ans in ("y", "yes")
        await client.post(
            f"{base}/sessions/{session_id}/tool-confirmations/{evt.tool_call_id}",
            json={"approved": approved, "reason": None if approved else "denied by user"},
        )
    elif isinstance(evt, ActionRequiredEvent):
        try:
            raw = input("Agent requests input (JSON or text): ").strip()
        except EOFError:
            raw = ""
        user_data: Any = raw
        if raw.startswith("{"):
            try:
                user_data = json.loads(raw)
            except json.JSONDecodeError:
                pass
        await client.post(
            f"{base}/sessions/{session_id}/elicitations/{evt.id}",
            json={"user_data": user_data},
        )
    elif isinstance(evt, FrontendToolRequestEvent):
        print(f"\x1b[33mFrontend tool '{evt.name}' requires a UI — not supported in terminal chat.\x1b[0m")


async def _chat_session(args: argparse.Namespace, base: str, *, spawned_gateway: bool) -> int:
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    interrupt = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, interrupt.set)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            health = await client.get(f"{base}/health")
            health.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Gateway not reachable at {base}: {exc}", file=sys.stderr)
            return 1

        body: dict[str, Any] = {}
        if args.model_provider:
            body["model_provider"] = args.model_provider
        if args.model_name:
            body["model_name"] = args.model_name
        try:
            create = await client.post(f"{base}/sessions", json=body)
            create.raise_for_status()
        except httpx.HTTPError as exc:
            print(_format_http_error("Session creation", exc), file=sys.stderr)
            return 1
        session_id = create.json()["session_id"]

        stream_task: asyncio.Task[None] | None = None
        event_queue: asyncio.Queue[str | None] = asyncio.Queue()
        stream_alive = True

        async def _consume_stream() -> None:
            nonlocal stream_alive
            try:
                async with client.stream("GET", f"{base}/sessions/{session_id}/events") as resp:
                    resp.raise_for_status()
                    async for payload in _iter_sse_lines(resp):
                        await event_queue.put(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stream_alive = False
                print(_format_http_error("Event stream", exc), file=sys.stderr)
                await event_queue.put(None)

        stream_task = asyncio.create_task(_consume_stream())
        _print_welcome(spawned_gateway=spawned_gateway)

        try:
            while not interrupt.is_set():
                user_line = await _read_line("you: ", interrupt)
                if user_line is None or interrupt.is_set():
                    break
                if not user_line.strip():
                    continue
                if _is_exit_command(user_line):
                    if spawned_gateway:
                        print(f"\n{_DIM}Goodbye — shutting down gateway…{_RESET}")
                    else:
                        print(f"\n{_DIM}Goodbye.{_RESET}")
                    break
                request_id = str(uuid.uuid4())
                try:
                    reply = await client.post(
                        f"{base}/sessions/{session_id}/reply",
                        json={"request_id": request_id, "message": user_line},
                    )
                    if reply.status_code == 409:
                        print("Session busy — wait for the current turn to finish.", file=sys.stderr)
                        continue
                    reply.raise_for_status()
                except httpx.HTTPError as exc:
                    print(_format_http_error("Reply", exc), file=sys.stderr)
                    continue
                print()

                spinner = _SpinnerLine("thinking…")
                spinner.start()
                activity = _TurnActivity()
                assistant_label_shown = False
                done = False
                while not done and not interrupt.is_set():
                    try:
                        payload = await asyncio.wait_for(event_queue.get(), timeout=0.15)
                    except TimeoutError:
                        continue
                    if payload is None:
                        await spinner.clear()
                        await activity.cancel()
                        break
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "ActiveRequests":
                        continue
                    try:
                        evt = event_from_json(payload)
                    except Exception:
                        continue
                    if isinstance(evt, (ToolConfirmationRequestEvent, ActionRequiredEvent, FrontendToolRequestEvent)):
                        await spinner.clear()
                        await activity.cancel()
                        await _handle_hitl(client, base, session_id, evt)
                        spinner.start()
                        continue
                    if isinstance(evt, ToolCallStarted) and evt.request_id == request_id:
                        if assistant_label_shown:
                            print()
                        else:
                            await spinner.clear()
                        await activity.tool_started(evt.tool, evt.label, evt.args)
                        continue
                    if isinstance(evt, ToolCallResult) and evt.request_id == request_id:
                        await activity.tool_finished(
                            evt.tool,
                            error=evt.error,
                            verbose=args.verbose,
                            result=evt.result or "",
                        )
                        continue
                    if isinstance(evt, ContextSummarizing) and evt.request_id == request_id:
                        if not assistant_label_shown:
                            await spinner.clear()
                        await activity.summarizing(evt.estimated_tokens)
                        continue
                    if isinstance(evt, ContextSummarized) and evt.request_id == request_id:
                        await activity.summarized(evt.turns_summarized)
                        continue
                    if isinstance(evt, AssistantDelta) and evt.request_id == request_id:
                        if not assistant_label_shown:
                            await spinner.clear()
                            await activity.cancel()
                            print("assistant: ", end="", flush=True)
                            assistant_label_shown = True
                        print(evt.delta, end="", flush=True)
                        continue
                    result = _print_event(
                        evt,
                        show_thinking=args.show_thinking,
                        show_usage=args.usage,
                        request_id=request_id,
                    )
                    if result in ("turn_complete", "error"):
                        if not assistant_label_shown:
                            await spinner.clear()
                        await activity.cancel()
                        if result == "turn_complete":
                            print()
                        done = True
                if interrupt.is_set():
                    await spinner.clear()
                    await activity.cancel()
                    break
                if not stream_alive:
                    break
        finally:
            if stream_task:
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(stream_task, timeout=0.5)
    return 0


class _SpawnedGateway(NamedTuple):
    proc: subprocess.Popen[str]
    log_path: Path
    log_file: TextIO


def _format_gateway_log_tail(log_path: Path, *, max_lines: int = 40) -> str:
    if not log_path.is_file():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


def _tail_gateway_log(gateway: _SpawnedGateway, *, max_lines: int = 40) -> str:
    if gateway.proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            gateway.proc.wait(timeout=1)
    else:
        gateway.proc.wait()
    gateway.log_file.flush()
    with contextlib.suppress(OSError):
        gateway.log_file.close()
    return _format_gateway_log_tail(gateway.log_path, max_lines=max_lines)


def _cleanup_gateway_log(gateway: _SpawnedGateway | None) -> None:
    if gateway is None:
        return
    with contextlib.suppress(OSError):
        if not gateway.log_file.closed:
            gateway.log_file.close()
    with contextlib.suppress(OSError):
        gateway.log_path.unlink(missing_ok=True)


def _spawn_gateway(config_path: Path | None, cwd: Path, port: int) -> _SpawnedGateway:
    env = os.environ.copy()
    if config_path is not None:
        env["MONKEYBOT_CONFIG"] = str(config_path)
    env["PORT"] = str(port)
    env.setdefault("LOG_LEVEL", "error")
    log_file = tempfile.NamedTemporaryFile(
        mode="w+",
        prefix="monkeybot-gateway-",
        suffix=".log",
        delete=False,
        encoding="utf-8",
        errors="replace",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "monkeybot.gateway.main"],
        env=env,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=log_file,
    )
    return _SpawnedGateway(proc=proc, log_path=Path(log_file.name), log_file=log_file)


def _wait_for_health(base: str, proc: subprocess.Popen[str] | None, timeout_s: float = 30.0) -> bool:
    """Poll the gateway /health until it responds or the spawned process exits."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            resp = httpx.get(f"{base}/health", timeout=2.0)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def run_chat(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)

    base = _resolve_base_url(args, config_path)
    attach = args.attach or bool(args.url)

    proc: subprocess.Popen[str] | None = None
    spawned: _SpawnedGateway | None = None
    if not attach:
        if config_path is None:
            print(
                "No monkeybot.yaml found in this directory. cd into an agent root "
                "(containing monkeybot_config/monkeybot.yaml) or pass --config / --attach.",
                file=sys.stderr,
            )
            return 1
        port = args.port if args.port else _port_from_config(config_path)
        spawned = _spawn_gateway(config_path, cwd, port)
        proc = spawned.proc
        if not _wait_for_health(base, proc):
            print("Gateway failed to start.", file=sys.stderr)
            log_tail = _tail_gateway_log(spawned)
            if log_tail:
                print(f"{_DIM}--- gateway log ---{_RESET}", file=sys.stderr)
                print(log_tail, file=sys.stderr)
                print(f"{_DIM}--- end log ---{_RESET}", file=sys.stderr)
            else:
                print("Run `monkeybot run` to see logs.", file=sys.stderr)
            if proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            _cleanup_gateway_log(spawned)
            return 1

    try:
        return asyncio.run(_chat_session(args, base, spawned_gateway=not attach))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        if not attach:
            print(f"{_DIM}Shutting down gateway…{_RESET}", file=sys.stderr)
        return 130
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
        if spawned is not None:
            _cleanup_gateway_log(spawned)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "chat",
        help="Talk to the agent (starts the gateway automatically from the current dir)",
    )
    p.add_argument("--cwd", help="Agent root (defaults to the current directory)")
    p.add_argument("--config", help="Path to monkeybot.yaml (defaults to ./monkeybot_config/monkeybot.yaml)")
    p.add_argument("--attach", action="store_true", help="Connect to an already-running gateway instead of spawning one")
    p.add_argument("--url", help="Gateway base URL (implies --attach; overrides config-derived port)")
    p.add_argument("--port", type=int, help="Gateway port (overrides runtime.port from config)")
    p.add_argument("--show-thinking", action="store_true", help="Show thinking events")
    p.add_argument("--verbose", action="store_true", help="Show tool result payloads after each tool")
    p.add_argument("--usage", action="store_true", help="Show token usage after each turn")
    p.add_argument("--model-provider", dest="model_provider", help="Override model provider for session")
    p.add_argument("--model-name", dest="model_name", help="Override model name for session")
    p.set_defaults(func=run_chat)
