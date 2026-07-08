"""MonkeyBot CLI entrypoint (realtime talk helpers).

The user-facing ``monkeybot`` console script lives in the ``monkeybot-cli`` package
(``cli/``). This module keeps the realtime ``talk`` implementation so both
``python -m monkeybot.cli`` and ``monkeybot talk`` share one code path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Annotated

import typer

from monkeybot.core.config.realtime_config import get_realtime_config
from monkeybot.core.logging_utils import normalize_log_level

from .audio_io import AudioIOError, AudioPlayer, AudioRecorder
from .gateway_manager import start_gateway_if_needed, stop_gateway
from .push_to_talk import PushToTalkError, PushToTalkGate
from .realtime_client import RealtimeCLIClient, RealtimeClientError

app = typer.Typer(help="MonkeyBot realtime CLI helpers (prefer the monkeybot-cli package)")


def _setup_logging(*, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else normalize_log_level(os.getenv("LOG_LEVEL"))
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
    )


def _generate_session_id() -> str:
    return uuid.uuid4().hex


def _parse_sample_rate(fmt: str) -> int:
    parts = fmt.lower().split("_")
    for p in parts:
        if p.endswith("khz"):
            try:
                return int(p.replace("khz", "")) * 1000
            except ValueError:
                pass
    return 24000


async def _run_talk(
    client: RealtimeCLIClient,
    gateway_url: str,
    *,
    start_gateway: bool,
) -> None:
    """Start the gateway if needed, run the client, and clean up."""
    gateway_proc = await start_gateway_if_needed(gateway_url, start=start_gateway)
    try:
        await client.run()
    finally:
        await stop_gateway(gateway_proc)


def run_talk_session(
    *,
    gateway_url: str = "ws://localhost:8787",
    session_id: str | None = None,
    text: bool = False,
    ptt_key: str = "cmd",
    start_gateway: bool = True,
    input_format: str = "pcm_s16le_24khz_mono",
    chunk_ms: int = 200,
    verbose: bool = False,
) -> int:
    """Run a realtime talk session. Returns a process exit code."""
    _setup_logging(verbose=verbose)
    get_realtime_config()
    if not session_id:
        session_id = _generate_session_id()

    audio_input_enabled = not text
    recorder: AudioRecorder | None = None
    player: AudioPlayer | None = None
    ptt: PushToTalkGate | None = None

    try:
        player = AudioPlayer(
            sample_rate=_parse_sample_rate(input_format),
            channels=1,
            format_name=input_format,
        )
    except AudioIOError as exc:
        if audio_input_enabled:
            print(f"Audio output setup failed: {exc}", file=sys.stderr)
        else:
            print(
                f"Audio output unavailable: {exc} Text mode will still work; "
                "transcripts are printed.",
                file=sys.stderr,
            )

    if audio_input_enabled:
        try:
            recorder = AudioRecorder(
                sample_rate=_parse_sample_rate(input_format),
                channels=1,
                chunk_ms=chunk_ms,
                format_name=input_format,
            )
        except AudioIOError as exc:
            if player is None:
                print(f"Audio setup failed: {exc}", file=sys.stderr)
                print(
                    "Tip: install PortAudio (brew install portaudio) and sync with "
                    "'uv sync --extra cli-realtime', or use --text for text-only mode.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"Microphone unavailable: {exc} Continuing with text input and audio output.",
                file=sys.stderr,
            )
            audio_input_enabled = False

    if audio_input_enabled:
        try:
            ptt = PushToTalkGate(key_name=ptt_key)
        except PushToTalkError as exc:
            print(f"Push-to-talk unavailable: {exc}", file=sys.stderr)
            print(
                "Tip: sync with 'uv sync --extra cli-realtime'. "
                "On macOS, grant Accessibility permission to your terminal app.",
                file=sys.stderr,
            )
            return 1

    client = RealtimeCLIClient(
        gateway_url=gateway_url,
        session_id=session_id,
        audio_enabled=audio_input_enabled,
        audio_recorder=recorder,
        audio_player=player,
        push_to_talk=ptt,
        verbose=verbose,
    )
    try:
        asyncio.run(_run_talk(client, gateway_url, start_gateway=start_gateway))
    except (RealtimeClientError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if recorder is not None:
            recorder.close()
        if player is not None:
            player.close()
    return 0


@app.command("talk")
def talk(
    gateway_url: Annotated[
        str,
        typer.Option(
            "--gateway-url",
            help="Base URL of the MonkeyBot realtime gateway, e.g. ws://localhost:8787",
            envvar="MONKEYBOT_GATEWAY_URL",
        ),
    ] = "ws://localhost:8787",
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help="Session ID to connect to; generated if not provided",
            envvar="MONKEYBOT_SESSION_ID",
        ),
    ] = None,
    text: Annotated[
        bool,
        typer.Option(
            "--text/--no-text",
            help="Text-only input (no microphone). Audio output is still used when available.",
        ),
    ] = False,
    ptt_key: Annotated[
        str,
        typer.Option(
            "--ptt-key",
            help="Push-to-talk key to hold while speaking: cmd, alt, ctrl, or space",
        ),
    ] = "cmd",
    start_gateway: Annotated[
        bool,
        typer.Option(
            "--start-gateway/--no-start-gateway",
            help="Start a local realtime gateway if one is not already reachable",
        ),
    ] = True,
    input_format: Annotated[
        str,
        typer.Option(
            "--input-format",
            help="Audio format, e.g. pcm_s16le_24khz_mono",
        ),
    ] = "pcm_s16le_24khz_mono",
    chunk_ms: Annotated[
        int,
        typer.Option("--chunk-ms", help="Audio chunk size in milliseconds"),
    ] = 200,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose/--no-verbose",
            help="Enable debug logging for audio chunks and gateway events",
        ),
    ] = False,
) -> None:
    """Talk with a MonkeyBot realtime agent.

    Audio is enabled by default with push-to-talk: hold ⌘ Command to speak, release to
    stop. The agent's audio replies play through your speakers.

    Example: monkeybot talk

    Use --text to disable the microphone and type your input instead. The agent still
    responds through audio if your system can play it; text transcripts are printed as well.

    By default, the CLI starts a local gateway if none is reachable at the --gateway-url.
    Use --no-start-gateway to connect to a gateway you started separately.

    Special commands:
      /interrupt  - send an interrupt signal
      /bye        - close the session and exit (/quit and /exit also work)
    """
    code = run_talk_session(
        gateway_url=gateway_url,
        session_id=session_id,
        text=text,
        ptt_key=ptt_key,
        start_gateway=start_gateway,
        input_format=input_format,
        chunk_ms=chunk_ms,
        verbose=verbose,
    )
    if code != 0:
        raise typer.Exit(code)


@app.command()
def version() -> None:
    """Show MonkeyBot version."""
    from importlib.metadata import version as get_version

    typer.echo(get_version("monkeybot"))


def main() -> None:
    """Console entrypoint for realtime helpers (prefer ``monkeybot`` from monkeybot-cli)."""
    app()


if __name__ == "__main__":
    main()
