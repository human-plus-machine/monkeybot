"""monkeybot talk — realtime WebSocket client (text and/or audio)."""

from __future__ import annotations

import argparse
import os
from typing import Any

from monkeybot.cli.main import run_talk_session


def run_talk(args: argparse.Namespace) -> int:
    """Connect to the realtime gateway (audio by default, or --text)."""
    return run_talk_session(
        gateway_url=args.gateway_url,
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
    p.add_argument(
        "--gateway-url",
        default=os.getenv("MONKEYBOT_GATEWAY_URL", "ws://localhost:8787"),
        help="Realtime gateway base URL (env: MONKEYBOT_GATEWAY_URL)",
    )
    p.add_argument(
        "--session-id",
        default=os.getenv("MONKEYBOT_SESSION_ID"),
        help="Session ID (generated if omitted; env: MONKEYBOT_SESSION_ID)",
    )
    p.add_argument(
        "--text",
        action="store_true",
        help="Text-only input (no microphone); audio output still used when available",
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
