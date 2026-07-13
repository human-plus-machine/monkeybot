"""monkeybot talk — realtime WebSocket client (text and/or audio)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from monkeybot_cli.config_resolve import load_config_doc, resolve_config
from monkeybot_cli.realtime.session import run_talk_session
from monkeybot_cli.runtime_python import DEFAULT_PORT


def _gateway_url_from_args(args: argparse.Namespace) -> str:
    if getattr(args, "gateway_url", None):
        return str(args.gateway_url).rstrip("/")
    env = os.getenv("MONKEYBOT_GATEWAY_URL")
    if env:
        return env.rstrip("/")
    cwd = Path(args.cwd).expanduser().resolve() if getattr(args, "cwd", None) else None
    config_path = resolve_config(getattr(args, "config", None), cwd=cwd)
    port = DEFAULT_PORT
    if config_path is not None:
        _, doc = load_config_doc(str(config_path))
        runtime = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
        try:
            port = int(runtime.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            port = DEFAULT_PORT
    return f"ws://127.0.0.1:{port}"


def run_talk(args: argparse.Namespace) -> int:
    """Connect to the realtime gateway (audio by default, or --text)."""
    return run_talk_session(
        gateway_url=_gateway_url_from_args(args),
        session_id=args.session_id,
        text=bool(args.text),
        ptt_key=args.ptt_key,
        start_gateway=not args.no_start_gateway,
        input_format=args.input_format,
        chunk_ms=args.chunk_ms,
        verbose=bool(args.verbose),
    )


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "talk",
        help="Talk with a realtime agent (WebSocket; audio by default)",
    )
    p.add_argument("--cwd", help="Agent root (defaults to the current directory)")
    p.add_argument(
        "--config", help="Path to monkeybot.yaml (defaults to ./monkeybot_config/monkeybot.yaml)"
    )
    p.add_argument(
        "--gateway-url",
        default=None,
        help=(
            "Realtime gateway base URL "
            f"(env: MONKEYBOT_GATEWAY_URL; default ws://127.0.0.1:{{runtime.port|{DEFAULT_PORT}}})"
        ),
    )
    p.add_argument(
        "--session-id",
        default=os.getenv("MONKEYBOT_SESSION_ID"),
        help="Session ID (generated if omitted; env: MONKEYBOT_SESSION_ID)",
    )
    p.add_argument(
        "--text",
        action="store_true",
        help="Text-only input (no microphone); uses the chat TUI when on a TTY",
    )
    p.add_argument(
        "--ptt-key",
        default="cmd",
        choices=("cmd", "alt", "ctrl", "space"),
        help="Push-to-talk key to hold while speaking (default: cmd)",
    )
    p.add_argument(
        "--no-start-gateway",
        action="store_true",
        help="Do not auto-start a local realtime gateway",
    )
    p.add_argument(
        "--input-format",
        default="pcm_s16le_24khz_mono",
        help="Audio format, e.g. pcm_s16le_24khz_mono",
    )
    p.add_argument(
        "--chunk-ms",
        type=int,
        default=200,
        help="Audio chunk size in milliseconds",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging for audio chunks and gateway events",
    )
    p.set_defaults(func=run_talk)
