"""Talk session orchestration (audio + text entrypoints)."""

from __future__ import annotations

import logging
import os
import sys
import uuid
from typing import Annotated

import typer
from monkeybot.core.config.realtime_config import get_realtime_config
from monkeybot.core.logging_utils import normalize_log_level

from monkeybot_cli.realtime.audio_io import AudioIOError, AudioPlayer, AudioRecorder
from monkeybot_cli.realtime.push_to_talk import PushToTalkError, PushToTalkGate
from monkeybot_cli.runtime_python import (
    DEFAULT_PORT,
    RuntimeUpgradeError,
    report_runtime_upgrade_error,
)

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


def default_gateway_ws_url() -> str:
    """Default WebSocket URL from env or ``runtime.port`` (DEFAULT_PORT)."""
    env = os.getenv("MONKEYBOT_GATEWAY_URL")
    if env:
        return env.rstrip("/")
    return f"ws://127.0.0.1:{DEFAULT_PORT}"


def _setup_audio_devices(
    *,
    text: bool,
    input_format: str,
    chunk_ms: int,
    ptt_key: str,
) -> tuple[bool, AudioRecorder | None, AudioPlayer | None, PushToTalkGate | None, int]:
    """Open player/recorder/PTT for talk. Returns (audio_enabled, recorder, player, ptt, exit_code).

    ``exit_code`` is non-zero when setup failed fatally (caller should return it).
    """
    audio_input_enabled = not text
    recorder: AudioRecorder | None = None
    player: AudioPlayer | None = None
    ptt: PushToTalkGate | None = None
    device_hints: list[str] = []

    if text:
        return False, None, None, None, 0

    try:
        player = AudioPlayer(
            sample_rate=_parse_sample_rate(input_format),
            channels=1,
            format_name=input_format,
        )
    except AudioIOError as exc:
        device_hints.append(f"Audio output unavailable: {exc}")

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
            return False, None, None, None, 1
        device_hints.append(f"Microphone unavailable: {exc} Continuing with text input.")
        audio_input_enabled = False

    if audio_input_enabled:
        try:
            ptt = PushToTalkGate(key_name=ptt_key)
        except PushToTalkError as exc:
            print(f"Push-to-talk unavailable: {exc}", file=sys.stderr)
            print(
                "Tip: sync with 'uv sync --extra cli-realtime'. "
                "On macOS, grant Accessibility permission to your terminal app. "
                "Space in an empty composer toggles in-TUI PTT.",
                file=sys.stderr,
            )
            # Continue without global PTT — in-TUI Space still works when TTY.
            ptt = None

    for hint in device_hints:
        print(hint, file=sys.stderr)

    return audio_input_enabled and recorder is not None, recorder, player, ptt, 0


def run_talk_session(
    *,
    gateway_url: str | None = None,
    session_id: str | None = None,
    text: bool = False,
    ptt_key: str = "cmd",
    start_gateway: bool = True,
    input_format: str = "pcm_s16le_24khz_mono",
    chunk_ms: int = 200,
    verbose: bool = False,
) -> int:
    """Run a realtime talk session via ``RealtimeSessionController``. Returns exit code."""
    _setup_logging(verbose=verbose)
    get_realtime_config()
    if not session_id:
        session_id = _generate_session_id()
    if not gateway_url:
        gateway_url = default_gateway_ws_url()

    from monkeybot_cli.realtime.talk_ui import run_talk_ui_session

    audio_enabled, recorder, player, ptt, setup_code = _setup_audio_devices(
        text=text,
        input_format=input_format,
        chunk_ms=chunk_ms,
        ptt_key=ptt_key,
    )
    if setup_code != 0:
        return setup_code

    try:
        return run_talk_ui_session(
            gateway_url=gateway_url,
            session_id=session_id,
            start_gateway=start_gateway,
            verbose=verbose,
            audio_enabled=audio_enabled,
            audio_recorder=recorder,
            audio_player=player,
            push_to_talk=ptt,
        )
    except RuntimeUpgradeError as exc:
        return report_runtime_upgrade_error(exc)
    finally:
        if recorder is not None:
            recorder.close()
        if player is not None:
            player.close()


@app.command("talk")
def talk(
    gateway_url: Annotated[
        str,
        typer.Option(
            "--gateway-url",
            help="Base URL of the MonkeyBot realtime gateway, e.g. ws://127.0.0.1:8080",
            envvar="MONKEYBOT_GATEWAY_URL",
        ),
    ] = "",
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
    """Talk with a MonkeyBot realtime agent."""
    code = run_talk_session(
        gateway_url=gateway_url or None,
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
    try:
        app()
    except RuntimeUpgradeError as exc:
        raise SystemExit(report_runtime_upgrade_error(exc)) from None


if __name__ == "__main__":
    main()
