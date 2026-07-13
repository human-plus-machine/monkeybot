"""Wire ``monkeybot talk`` (text or audio) through the shared Chat TUI / plain renderer."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from monkeybot_cli.chat_tui import is_exit_command, run_chat_tui
from monkeybot_cli.commands.chat import (
    _DIM,
    _RESET,
    _USER_PROMPT,
    _PlainRenderer,
    _read_line,
    use_textual_tui,
)
from monkeybot_cli.config_resolve import (
    load_agent_dotenv,
    load_config_doc,
    resolve_agent_root,
    resolve_config,
)
from monkeybot_cli.gateway_health import health_ok, wait_for_health
from monkeybot_cli.realtime.session_controller import RealtimeSessionController
from monkeybot_cli.runtime_python import (
    COMBINED_GATEWAY_MODULE,
    DEFAULT_PORT,
    gateway_argv,
    resolve_runtime_python,
)


class _SpawnedGateway(NamedTuple):
    proc: subprocess.Popen[str]
    log_path: Path
    log_file: TextIO


def _ws_to_http_base(gateway_url: str) -> str:
    parsed = urllib.parse.urlparse(gateway_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or DEFAULT_PORT
    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{host}:{port}"


def _model_fields(config_path: Path | None) -> tuple[str, str]:
    provider, model = "?", "?"
    if config_path is None:
        return provider, model
    _, doc = load_config_doc(str(config_path))
    model_cfg = doc.get("model") if isinstance(doc.get("model"), dict) else {}
    provider = str(model_cfg.get("provider") or provider).strip() or provider
    model = str(model_cfg.get("name") or model).strip() or model
    return provider, model


def _url_is_local(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _spawn_combined_gateway(
    config_path: Path | None, agent_root: Path, port: int
) -> _SpawnedGateway:
    import os

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
        gateway_argv(resolve_runtime_python(agent_root), module=COMBINED_GATEWAY_MODULE),
        env=env,
        cwd=agent_root,
        stdout=subprocess.DEVNULL,
        stderr=log_file,
    )
    return _SpawnedGateway(proc=proc, log_path=Path(log_file.name), log_file=log_file)


def _cleanup_gateway(spawned: _SpawnedGateway | None) -> None:
    if spawned is None:
        return
    if spawned.proc.poll() is None:
        spawned.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            spawned.proc.wait(timeout=1)
    with contextlib.suppress(OSError):
        if not spawned.log_file.closed:
            spawned.log_file.close()
    with contextlib.suppress(OSError):
        spawned.log_path.unlink(missing_ok=True)


async def _plain_talk_session(
    *,
    controller: RealtimeSessionController,
    spawned_gateway: bool,
) -> int:
    interrupt = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, interrupt.set)

    renderer = _PlainRenderer(animations_enabled=True)
    renderer.start_io_worker()
    controller.set_emit(lambda e: renderer.on_event(e, controller))

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
                break
            await controller.submit(user_line)
    finally:
        await controller.close()
        await renderer.stop_io_worker()
    return 1 if controller.stream_error else 0


def run_talk_ui_session(
    *,
    gateway_url: str,
    session_id: str | None = None,
    start_gateway: bool = True,
    verbose: bool = False,
    audio_enabled: bool = False,
    audio_recorder: Any | None = None,
    audio_player: Any | None = None,
    push_to_talk: Any | None = None,
) -> int:
    """Run talk via ChatApp (TTY) or plain renderer, with optional audio I/O."""
    cwd = Path.cwd()
    config_path = resolve_config(None, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    agent_root = resolve_agent_root(cwd=cwd, config_path=config_path)
    provider, model = _model_fields(config_path)
    base = _ws_to_http_base(gateway_url)
    sid = session_id or uuid.uuid4().hex
    parsed = urllib.parse.urlparse(gateway_url)
    port = parsed.port or DEFAULT_PORT

    spawned: _SpawnedGateway | None = None
    if start_gateway and _url_is_local(gateway_url):
        if not health_ok(base):
            if config_path is None:
                print(
                    "Could not find monkeybot_config/monkeybot.yaml to start the gateway. "
                    "Run the command from a MonkeyBot workspace or start the gateway manually.",
                    file=sys.stderr,
                )
                return 1
            spawned = _spawn_combined_gateway(config_path, agent_root, port)
            if not wait_for_health(base, spawned.proc):
                print("Gateway failed to start.", file=sys.stderr)
                _cleanup_gateway(spawned)
                return 1

    controller = RealtimeSessionController(
        gateway_url=gateway_url,
        session_id=sid,
        verbose=verbose,
        audio_enabled=audio_enabled,
        audio_recorder=audio_recorder,
        audio_player=audio_player,
        push_to_talk=push_to_talk,
    )

    try:
        if use_textual_tui():
            return run_chat_tui(
                base=base,
                agent_root=agent_root,
                provider=provider,
                model=model,
                spawned_gateway=spawned is not None,
                verbose=verbose,
                controller=controller,
            )
        return asyncio.run(
            _plain_talk_session(controller=controller, spawned_gateway=spawned is not None)
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 130
    finally:
        _cleanup_gateway(spawned)


def run_talk_text_session(
    *,
    gateway_url: str,
    session_id: str | None = None,
    start_gateway: bool = True,
    verbose: bool = False,
) -> int:
    """Backward-compatible text-only entry."""
    return run_talk_ui_session(
        gateway_url=gateway_url,
        session_id=session_id,
        start_gateway=start_gateway,
        verbose=verbose,
        audio_enabled=False,
    )
