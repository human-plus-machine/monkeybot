"""monkeybot chat — gateway client (Textual TUI or plain fallback)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, NamedTuple, TextIO

import httpx

from monkeybot_cli.chat_session import (
    ChatSessionController,
    ChatUiEvent,
    HitlAnswer,
    format_hitl_plain_prompt,
)
from monkeybot_cli.chat_status_bar import (
    SessionUsageView,
    format_context_ring,
    format_context_ring_plain,
)
from monkeybot_cli.chat_tool_display import (
    tool_display,
    tool_spinner_prefix,
)
from monkeybot_cli.chat_tui import is_exit_command, run_chat_tui
from monkeybot_cli.config_resolve import (
    load_agent_dotenv,
    load_config_doc,
    resolve_agent_root,
    resolve_config,
)
from monkeybot_cli.gateway_health import (
    occupied_gateway_message as _occupied_gateway_message,
    wait_for_health as _wait_for_health,
)
from monkeybot_cli.opensandbox_lifecycle import (
    ensure_opensandbox_for_agent,
    is_sandbox_enabled,
    server_url_from_config,
)
from monkeybot_cli.runtime_python import DEFAULT_PORT, gateway_argv, resolve_runtime_python
from monkeybot_cli.terminal_markdown import MarkdownPlainStream

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"
_USER_PROMPT = "🧑"
_ASSISTANT_PREFIX = "🐵 "
_CYAN = "\x1b[36m"
_UNDERLINE = "\x1b[4m"
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_GROUNDING_SOURCES_MAX = 5

def _hyperlink(url: str, label: str) -> str:
    """OSC 8 terminal hyperlink; falls back to a plain URL on non-TTY streams."""
    if not url:
        return label
    if not sys.stdout.isatty():
        return f"{label} ({url})" if label and label != url else url
    return f"\x1b]8;;{url}\x1b\\{label or url}\x1b]8;;\x1b\\"


def _print_grounding(evt: Any) -> None:
    if not getattr(evt, "sources", None) and not getattr(evt, "search_queries", None):
        return
    header = "grounded search"
    queries = getattr(evt, "search_queries", None) or []
    if queries:
        header += " — " + ", ".join(f'"{q}"' for q in queries)
    print(f"{_DIM}  🔎 {header}{_RESET}", flush=True)
    sources = list(getattr(evt, "sources", None) or [])
    for source in sources[:_GROUNDING_SOURCES_MAX]:
        title = source.get("title", "").strip()
        uri = source.get("uri", "").strip()
        if not uri:
            continue
        link = _hyperlink(uri, title or uri)
        print(f"{_DIM}    {_CYAN}{_UNDERLINE}{link}{_RESET}", flush=True)
    remaining = len(sources) - _GROUNDING_SOURCES_MAX
    if remaining > 0:
        print(f"{_DIM}    … and {remaining} more{_RESET}", flush=True)


def use_textual_tui() -> bool:
    if os.environ.get("MONKEYBOT_CHAT_PLAIN", "").strip().lower() in ("1", "true", "yes"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


class _SpinnerLine:
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


class _TurnActivity:
    """Animated status lines for tools (plain path + unit tests)."""

    def __init__(self) -> None:
        self._line: _SpinnerLine | None = None
        self._active_display = ""

    async def _start(self, prefix: str) -> None:
        if self._line is not None:
            await self._line.clear()
        self._line = _SpinnerLine(prefix)
        self._line.start()

    async def tool_started(
        self,
        tool: str,
        label: str | None = None,
        args: dict[str, object] | None = None,
        *,
        display: str | None = None,
        prefix: str | None = None,
    ) -> None:
        # Compat: tests call tool_started(tool, label, args)
        if display is None and label is not None and args is not None:
            display = tool_display(tool, label, args)
            prefix = tool_spinner_prefix(tool, label, args)
        self._active_display = display or tool
        await self._start(prefix or display or tool)

    async def tool_finished(
        self,
        tool: str | None = None,
        *,
        error: str | None = None,
        verbose: bool = False,
        result: str = "",
    ) -> None:
        display = self._active_display or tool or "tool"
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
        if verbose and (error or result):
            print(f"{_DIM}    {error or result}{_RESET}")

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


def _finish_assistant_turn(
    *,
    md_stream: MarkdownPlainStream,
    assistant_label_shown: bool,
    show_usage: bool,
    usage: Any | None = None,
    error: str | None = None,
) -> None:
    if assistant_label_shown:
        tail = md_stream.flush()
        if tail:
            print(tail, end="", flush=True)
    if error:
        print(f"\n{_RED}Error: {error}{_RESET}", flush=True)
    elif assistant_label_shown or error:
        print()
    if show_usage and usage is not None:
        print(
            f"{_DIM}[usage] in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
            f"cost=${usage.get('cost_usd'):.4f} {usage.get('duration_ms')}ms{_RESET}"
        )
    print()


async def _read_line(prompt: str, interrupt: asyncio.Event) -> str | None:
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


def _port_from_config(config_path: Path | None) -> int:
    if config_path is None:
        return DEFAULT_PORT
    _, doc = load_config_doc(str(config_path))
    runtime = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
    try:
        return int(runtime.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _resolve_base_url(args: argparse.Namespace, config_path: Path | None) -> str:
    if args.url:
        return args.url.rstrip("/")
    port = args.port if args.port else _port_from_config(config_path)
    return f"http://127.0.0.1:{port}"


def _model_banner_fields(
    args: argparse.Namespace, config_path: Path | None
) -> tuple[str, str]:
    provider = (args.model_provider or "").strip()
    model = (args.model_name or "").strip()
    if config_path is not None and (not provider or not model):
        _, doc = load_config_doc(str(config_path))
        model_cfg = doc.get("model") if isinstance(doc.get("model"), dict) else {}
        if not provider:
            provider = str(model_cfg.get("provider") or "").strip()
        if not model:
            model = str(model_cfg.get("name") or "").strip()
    return provider or "?", model or "?"


def _print_context_ring(usage: SessionUsageView) -> None:
    """Print a context-window ring line for the plain path."""
    if sys.stdout.isatty():
        ring = format_context_ring(
            estimated_prompt_tokens=usage.estimated_prompt_tokens,
            last_prompt_tokens=usage.last_prompt_tokens,
            context_window_tokens=usage.context_window_tokens,
            summarization_threshold_tokens=usage.summarization_threshold_tokens,
        )
        print(ring, flush=True)
    else:
        print(format_context_ring_plain(usage), flush=True)


class _PlainRenderer:
    """Print-based sink for the non-TTY / CI path."""

    def __init__(self, *, animations_enabled: bool = True) -> None:
        self.animations_enabled = animations_enabled
        self.spinner = _SpinnerLine("thinking…")
        self.activity = _TurnActivity()
        self.md = MarkdownPlainStream()
        self.assistant_open = False
        self.hitl_prompt: str | None = None
        self._pending_hitl: asyncio.Future[HitlAnswer] | None = None
        self._last_usage: SessionUsageView | None = None
        self._thinking_open = False
        self._io_queue: asyncio.Queue[Coroutine[Any, Any, None] | None] = asyncio.Queue()
        self._io_worker: asyncio.Task[None] | None = None

    def start_io_worker(self) -> None:
        self._io_worker = asyncio.create_task(self._drain_io())

    async def stop_io_worker(self) -> None:
        await self._io_queue.put(None)
        if self._io_worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._io_worker
            self._io_worker = None

    def _schedule(self, coro: Coroutine[Any, Any, None]) -> None:
        self._io_queue.put_nowait(coro)

    async def _drain_io(self) -> None:
        while True:
            item = await self._io_queue.get()
            if item is None:
                return
            await item

    def on_event(self, event: ChatUiEvent, controller: Any) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event.payload, controller)

    def _on_turn_started(self, _p: dict, _controller: Any) -> None:
        self.assistant_open = False
        self._thinking_open = False
        self.md = MarkdownPlainStream()
        print()
        self.spinner = _SpinnerLine("thinking…")
        if self.animations_enabled:
            self.spinner.start()
        else:
            print(f"{_DIM}  thinking…{_RESET}", flush=True)
        self.activity = _TurnActivity()

    def _on_thinking_clear(self, _p: dict, _controller: Any) -> None:
        self._schedule(self.spinner.clear())
        self._schedule(self.activity.cancel())

    def _on_assistant_start(self, _p: dict, _controller: Any) -> None:
        if self._thinking_open:
            self._on_thinking_block_complete({}, _controller)
        print(_ASSISTANT_PREFIX, end="", flush=True)
        self.assistant_open = True

    def _on_assistant_delta(self, p: dict, _controller: Any) -> None:
        rendered = self.md.feed(str(p.get("delta", "")))
        if rendered:
            print(rendered, end="", flush=True)

    def _on_tool_started(self, p: dict, _controller: Any) -> None:
        tool = str(p.get("tool") or "tool")
        label = str(p.get("label") or tool)
        raw_args = p.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        self._schedule(
            self.activity.tool_started(
                tool,
                display=tool_display(tool, label, args),
                prefix=tool_spinner_prefix(tool, label, args),
            )
        )

    def _on_tool_finished(self, p: dict, _controller: Any) -> None:
        self._schedule(
            self.activity.tool_finished(
                str(p.get("tool") or "tool"),
                error=p.get("error"),
                verbose=bool(p.get("verbose")),
                result=str(p.get("result") or ""),
            )
        )

    def _on_summarizing(self, p: dict, _controller: Any) -> None:
        self._schedule(self.activity.summarizing(int(p.get("tokens") or 0)))

    def _on_summarized(self, p: dict, _controller: Any) -> None:
        self._schedule(self.activity.summarized(int(p.get("turns") or 0)))

    def _on_grounding(self, p: dict, _controller: Any) -> None:
        queries = p.get("search_queries") or []
        header = "grounded search"
        if queries:
            header += " — " + ", ".join(f'"{q}"' for q in queries)
        print(f"{_DIM}  🔎 {header}{_RESET}", flush=True)
        for source in (p.get("sources") or [])[:5]:
            if isinstance(source, dict) and source.get("uri"):
                title = str(source.get("title") or "").strip()
                uri = str(source.get("uri") or "").strip()
                print(f"{_DIM}    {_CYAN}{_UNDERLINE}{title or uri} ({uri}){_RESET}", flush=True)

    def _on_turn_error(self, p: dict, _controller: Any) -> None:
        _finish_assistant_turn(
            md_stream=self.md,
            assistant_label_shown=self.assistant_open,
            show_usage=False,
            error=str(p.get("error") or ""),
        )

    def _on_turn_complete(self, p: dict, _controller: Any) -> None:
        _finish_assistant_turn(
            md_stream=self.md,
            assistant_label_shown=self.assistant_open,
            show_usage=bool(p.get("usage")),
            usage=p.get("usage"),
        )

    def _on_turn_aborted(self, p: dict, _controller: Any) -> None:
        cancel_ok = p.get("cancel_ok")
        if cancel_ok is False:
            print(f"{_DIM}Turn aborted locally (cancel failed){_RESET}\n", flush=True)
        else:
            print(
                f"{_DIM}Turn aborted — cancel sent; server may still finish{_RESET}\n",
                flush=True,
            )

    def _on_hitl_required(self, p: dict, _controller: Any) -> None:
        from monkeybot_cli.chat_session import HitlRequest

        kind = str(p.get("hitl_kind") or "confirm")
        if kind not in ("confirm", "elicit", "frontend_unsupported"):
            kind = "confirm"
        raw_schema = p.get("schema")
        schema = dict(raw_schema) if isinstance(raw_schema, dict) else None
        raw_args = p.get("arguments")
        arguments = dict(raw_args) if isinstance(raw_args, dict) else {}
        timeout_raw = p.get("timeout_sec")
        try:
            timeout_sec = float(timeout_raw) if timeout_raw is not None else None
        except (TypeError, ValueError):
            timeout_sec = None
        req = HitlRequest(
            kind=kind,  # type: ignore[arg-type]
            prompt=str(p.get("prompt") or ""),
            tool_call_id=str(p.get("tool_call_id") or ""),
            elicitation_id=str(p.get("elicitation_id") or ""),
            tool_name=str(p.get("tool_name") or ""),
            schema=schema,
            arguments=arguments,
            timeout_sec=timeout_sec,
        )
        self.hitl_prompt = format_hitl_plain_prompt(req)
        print(f"{_DIM}{self.hitl_prompt}{_RESET}", flush=True)

    def _on_hitl_failed(self, p: dict, _controller: Any) -> None:
        print(f"{_RED}{p.get('message')}{_RESET}", flush=True)

    def _on_hitl_frontend_unsupported(self, p: dict, _controller: Any) -> None:
        print(
            f"\x1b[33mFrontend tool '{p.get('name')}' requires a UI — "
            f"not supported in terminal chat.\x1b[0m"
        )

    def _on_session_busy(self, _p: dict, _controller: Any) -> None:
        print("Session busy — wait for the current turn to finish.", file=sys.stderr)

    def _on_error(self, p: dict, _controller: Any) -> None:
        print(str(p.get("message") or ""), file=sys.stderr)

    def _on_stream_failed(self, p: dict, _controller: Any) -> None:
        print(str(p.get("message") or ""), file=sys.stderr)

    def _on_thinking_trace(self, p: dict, _controller: Any) -> None:
        print(f"{_DIM}[thinking] {p.get('text')}{_RESET}", flush=True)

    def _on_thinking_block_delta(self, p: dict, _controller: Any) -> None:
        text = str(p.get("text") or "")
        if not text:
            return
        if not self._thinking_open:
            self.spinner.stop()
            print(f"{_DIM}{_BOLD}Thinking...{_RESET}", flush=True)
            self._thinking_open = True
        print(f"{_DIM}{text}{_RESET}", end="", flush=True)

    def _on_thinking_block_complete(self, _p: dict, _controller: Any) -> None:
        if self._thinking_open:
            print(flush=True)
            print(f"{_DIM}{_BOLD}...done thinking.{_RESET}", flush=True)
            self._thinking_open = False

    def _on_usage_updated(self, p: dict, _controller: Any) -> None:
        usage = p.get("usage")
        if isinstance(usage, SessionUsageView):
            self._last_usage = usage
            _print_context_ring(usage)

    # Intentional no-ops for TUI-only / optional plain events (parity with EVENT_KINDS).
    def _on_session_ready(self, _p: dict, _controller: Any) -> None:
        return

    def _on_connection_state(self, _p: dict, _controller: Any) -> None:
        return

    def _on_transcript_backfill(self, _p: dict, _controller: Any) -> None:
        return

    def _on_thinking(self, _p: dict, _controller: Any) -> None:
        return

    def _on_stream_ended(self, _p: dict, _controller: Any) -> None:
        return

    def _on_voice_state(self, _p: dict, _controller: Any) -> None:
        return

    def _on_audio_chunk(self, _p: dict, _controller: Any) -> None:
        return

    def _on_device_error(self, p: dict, _controller: Any) -> None:
        msg = str(p.get("message") or "Audio device error")
        hint = p.get("hint")
        print(f"{_DIM}{msg}{_RESET}", flush=True)
        if hint:
            print(f"{_DIM}{hint}{_RESET}", flush=True)

    def _on_user_transcript(self, p: dict, _controller: Any) -> None:
        text = str(p.get("text") or "").strip()
        if text and p.get("is_final", True):
            print(f"{_USER_PROMPT} {text}", flush=True)


async def _plain_chat_session(
    args: argparse.Namespace,
    base: str,
    *,
    spawned_gateway: bool,
) -> int:
    interrupt = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, interrupt.set)

    animations_enabled = not (
        bool(getattr(args, "no_animations", False))
        or os.environ.get("MONKEYBOT_CHAT_NO_ANIMATIONS", "").strip().lower()
        in ("1", "true", "yes")
    )
    renderer = _PlainRenderer(animations_enabled=animations_enabled)
    renderer.start_io_worker()

    async def hitl_reader(req: Any) -> HitlAnswer:
        from monkeybot_cli.chat_session import HitlRequest

        assert isinstance(req, HitlRequest)
        # Prompt already printed via _on_hitl_required; short line for input.
        label = "y/n" if req.kind == "confirm" else "input"
        ans = await _read_line(f"{label}: ", interrupt)
        if ans is None or interrupt.is_set():
            return HitlAnswer(cancelled=True)
        lower = ans.strip().lower()
        if req.kind == "confirm":
            if lower in ("y", "yes"):
                return HitlAnswer(approved=True, text=ans)
            return HitlAnswer(approved=False, text=ans)
        return HitlAnswer(text=ans)

    controller = ChatSessionController(
        base=base,
        model_provider=args.model_provider,
        model_name=args.model_name,
        show_thinking=args.show_thinking,
        verbose=args.verbose,
        show_usage=args.usage,
        emit=lambda e: renderer.on_event(e, controller),
        hitl_reader=hitl_reader,
        resume_session_id=getattr(args, "session", None),
    )
    try:
        await controller.connect()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        await renderer.stop_io_worker()
        return 1

    hint = "Type /bye to exit"
    if spawned_gateway:
        hint += " (stops the gateway)"
    print(f"{_DIM}{hint}. Ctrl-C also exits.{_RESET}\n")

    try:
        while not interrupt.is_set() and controller.stream_alive:
            user_line = await _read_line(_USER_PROMPT, interrupt)
            if user_line is None or interrupt.is_set():
                break
            if not user_line.strip():
                continue
            if is_exit_command(user_line):
                if spawned_gateway:
                    print(f"\n{_DIM}Goodbye — shutting down gateway…{_RESET}")
                else:
                    print(f"\n{_DIM}Goodbye.{_RESET}")
                if controller.session_id:
                    print(
                        f"{_DIM}To continue this conversation, run: monkeybot chat --continue{_RESET}"
                    )
                break
            await controller.submit(user_line)
    finally:
        await controller.close()
        if controller.transcript_report_dir:
            print(
                f"{_DIM}Transcript report → {controller.transcript_report_dir}{_RESET}",
                flush=True,
            )
        await renderer.stop_io_worker()
    return 1 if controller.stream_error else 0


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


def _spawn_gateway(config_path: Path | None, agent_root: Path, port: int) -> _SpawnedGateway:
    env = os.environ.copy()
    if config_path is not None:
        env["MONKEYBOT_CONFIG"] = str(config_path)
    env["PORT"] = str(port)
    env.setdefault("LOG_LEVEL", "error")
    env.setdefault("MONKEYBOT_TRANSCRIPT_ENABLED", "1")
    log_file = tempfile.NamedTemporaryFile(
        mode="w+",
        prefix="monkeybot-gateway-",
        suffix=".log",
        delete=False,
        encoding="utf-8",
        errors="replace",
    )
    proc = subprocess.Popen(
        gateway_argv(resolve_runtime_python(agent_root)),
        env=env,
        cwd=agent_root,
        stdout=subprocess.DEVNULL,
        stderr=log_file,
    )
    return _SpawnedGateway(proc=proc, log_path=Path(log_file.name), log_file=log_file)


def _resolve_continue_session_id(base: str) -> str | None:
    """Look up the most recently used session id for this agent root, if any."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{base}/api/chat-history", params={"limit": 1})
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"{_DIM}Could not fetch chat history ({exc}) — starting a new session.{_RESET}",
            file=sys.stderr,
        )
        return None
    threads = resp.json().get("threads") or []
    if not threads:
        return None
    sid = threads[0].get("session_id")
    return str(sid) if sid else None


def run_chat(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    agent_root = resolve_agent_root(cwd=cwd, config_path=config_path)

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
        _, cfg_doc = load_config_doc(config_path)
        if is_sandbox_enabled(cfg_doc):
            if not ensure_opensandbox_for_agent(
                agent_root,
                server_url=server_url_from_config(cfg_doc),
                docker_wait_secs=2.0,
            ):
                print(
                    f"{_DIM}Continuing without a healthy OpenSandbox — run_command may fail.{_RESET}",
                    file=sys.stderr,
                )
        port = args.port if args.port else _port_from_config(config_path)
        occupied = _occupied_gateway_message(base, port)
        if occupied is not None:
            print(occupied, file=sys.stderr)
            return 1
        spawned = _spawn_gateway(config_path, agent_root, port)
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

    if getattr(args, "resume_last", False) and not getattr(args, "session", None):
        resolved = _resolve_continue_session_id(base)
        if resolved:
            print(f"{_DIM}Continuing session {resolved[:8]}…{_RESET}")
            args.session = resolved
        else:
            print(f"{_DIM}No previous conversation found — starting a new session.{_RESET}")

    provider, model = _model_banner_fields(args, config_path)
    animations_enabled = not (
        bool(getattr(args, "no_animations", False))
        or os.environ.get("MONKEYBOT_CHAT_NO_ANIMATIONS", "").strip().lower()
        in ("1", "true", "yes")
    )
    theme_choice = str(getattr(args, "theme", "auto") or "auto")
    try:
        if use_textual_tui():
            return run_chat_tui(
                base=base,
                agent_root=agent_root,
                provider=provider,
                model=model,
                spawned_gateway=not attach,
                model_provider=args.model_provider,
                model_name=args.model_name,
                show_thinking=args.show_thinking,
                verbose=args.verbose,
                show_usage=args.usage,
                resume_session_id=getattr(args, "session", None),
                animations_enabled=animations_enabled,
                theme_choice=theme_choice,
                config_path=config_path,
            )
        return asyncio.run(_plain_chat_session(args, base, spawned_gateway=not attach))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        if not attach:
            print(f"{_DIM}Shutting down gateway…{_RESET}", file=sys.stderr)
        return 130
    finally:
        if proc and proc.poll() is None:
            # Prefer graceful shutdown so gateway lifespan can analyze open sessions.
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
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
    p.add_argument(
        "--config", help="Path to monkeybot.yaml (defaults to ./monkeybot_config/monkeybot.yaml)"
    )
    p.add_argument(
        "--attach",
        action="store_true",
        help="Connect to an already-running gateway instead of spawning one",
    )
    p.add_argument(
        "--url", help="Gateway base URL (implies --attach; overrides config-derived port)"
    )
    p.add_argument("--port", type=int, help="Gateway port (overrides runtime.port from config)")
    p.add_argument("--show-thinking", action="store_true", help="Show thinking events")
    p.add_argument(
        "--verbose", action="store_true", help="Show tool result payloads after each tool"
    )
    p.add_argument("--usage", action="store_true", help="Show token usage after each turn")
    p.add_argument(
        "--session",
        dest="session",
        help="Resume an existing gateway session id (with transcript backfill when available)",
    )
    p.add_argument(
        "--continue",
        "-c",
        dest="resume_last",
        action="store_true",
        help="Resume the most recent conversation for this agent root",
    )
    p.add_argument(
        "--model-provider", dest="model_provider", help="Override model provider for session"
    )
    p.add_argument("--model-name", dest="model_name", help="Override model name for session")
    p.add_argument(
        "--no-animations",
        action="store_true",
        help="Disable decorative spinners / batched markdown flush (also: MONKEYBOT_CHAT_NO_ANIMATIONS=1)",
    )
    p.add_argument(
        "--theme",
        choices=("auto", "dark", "light"),
        default="auto",
        help="Chat TUI theme (auto uses COLORFGBG when set; default dark)",
    )
    p.set_defaults(func=run_chat)
