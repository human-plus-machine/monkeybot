"""Parsed ``realtime`` section of monkeybot.yaml with defaults."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict


@dataclass(frozen=True)
class RealtimeAudioConfig:
    input_format: str = "pcm_s16le_24khz_mono"
    output_format: str = "pcm_s16le_24khz_mono"
    chunk_ms: int = 200
    max_utterance_sec: int = 60


@dataclass(frozen=True)
class RealtimeSessionConfig:
    max_duration_sec: int = 1800
    idle_timeout_sec: int = 120
    max_response_turn_sec: int = 300
    max_concurrent_sessions: int = 100


@dataclass(frozen=True)
class RealtimeWebSocketConfig:
    enabled: bool = True
    port: int | None = None


@dataclass(frozen=True)
class RealtimeMetricsConfig:
    emit_summary_on_close: bool = True


@dataclass(frozen=True)
class RealtimeModelConfig:
    """Override the model/provider used for the realtime session.

    Defaults to the main ``model`` section, but some live preview models (e.g.
    gemini-3.1-flash-live-preview) only support the live API and must be kept separate
    from turn-based providers like context curation and memory organizer.
    """

    name: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class RealtimeConfig:
    enabled: bool = False
    websocket: RealtimeWebSocketConfig = field(default_factory=RealtimeWebSocketConfig)
    audio: RealtimeAudioConfig = field(default_factory=RealtimeAudioConfig)
    session: RealtimeSessionConfig = field(default_factory=RealtimeSessionConfig)
    metrics: RealtimeMetricsConfig = field(default_factory=RealtimeMetricsConfig)
    model: RealtimeModelConfig = field(default_factory=RealtimeModelConfig)


def _nested_get(mapping: Any, *path: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    current: Any = mapping
    for part in path:
        if not isinstance(current, dict):
            return default
        current = current.get(part, default)
        if current is None:
            return default
    return current


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return default


def _to_int(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _overlay_bool(env: Mapping[str, str], key: str, current: bool) -> bool:
    if key not in env:
        return current
    return _to_bool(env[key], default=current)


def _overlay_int(env: Mapping[str, str], key: str, current: int) -> int:
    if key not in env:
        return current
    return _to_int(env[key], default=current)


def realtime_config_from_doc(
    doc: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> RealtimeConfig:
    """Build ``RealtimeConfig`` from a YAML doc, optionally overlaying snapshot env."""
    env = env or {}
    harness_raw = doc.get("harness")
    harness: dict[str, Any] = harness_raw if isinstance(harness_raw, dict) else {}
    mode = str(harness.get("mode", "turn_based")).strip().lower()
    if "MONKEYBOT_HARNESS_MODE" in env:
        mode = env["MONKEYBOT_HARNESS_MODE"].strip().lower()
    realtime_raw = doc.get("realtime")
    realtime: dict[str, Any] = realtime_raw if isinstance(realtime_raw, dict) else {}

    ws_port_raw = _to_int(_nested_get(realtime, "websocket", "port"), default=0)
    if "MONKEYBOT_REALTIME_WS_PORT" in env:
        ws_port_raw = _to_int(env["MONKEYBOT_REALTIME_WS_PORT"], default=ws_port_raw)

    return RealtimeConfig(
        enabled=mode == "realtime",
        websocket=RealtimeWebSocketConfig(
            enabled=_overlay_bool(
                env,
                "MONKEYBOT_REALTIME_WS_ENABLED",
                _to_bool(_nested_get(realtime, "websocket", "enabled"), default=True),
            ),
            port=ws_port_raw or None,
        ),
        audio=RealtimeAudioConfig(
            input_format=(
                env.get("MONKEYBOT_REALTIME_AUDIO_INPUT_FORMAT")
                or str(
                    _nested_get(realtime, "audio", "input_format", default="pcm_s16le_24khz_mono")
                )
            ),
            output_format=(
                env.get("MONKEYBOT_REALTIME_AUDIO_OUTPUT_FORMAT")
                or str(
                    _nested_get(realtime, "audio", "output_format", default="pcm_s16le_24khz_mono")
                )
            ),
            chunk_ms=_overlay_int(
                env,
                "MONKEYBOT_REALTIME_AUDIO_CHUNK_MS",
                _to_int(_nested_get(realtime, "audio", "chunk_ms"), default=200),
            ),
            max_utterance_sec=_overlay_int(
                env,
                "MONKEYBOT_REALTIME_AUDIO_MAX_UTTERANCE_SEC",
                _to_int(_nested_get(realtime, "audio", "max_utterance_sec"), default=60),
            ),
        ),
        session=RealtimeSessionConfig(
            max_duration_sec=_overlay_int(
                env,
                "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC",
                _to_int(_nested_get(realtime, "session", "max_duration_sec"), default=1800),
            ),
            idle_timeout_sec=_overlay_int(
                env,
                "MONKEYBOT_REALTIME_SESSION_IDLE_TIMEOUT_SEC",
                _to_int(_nested_get(realtime, "session", "idle_timeout_sec"), default=120),
            ),
            max_response_turn_sec=_overlay_int(
                env,
                "MONKEYBOT_REALTIME_SESSION_MAX_RESPONSE_TURN_SEC",
                _to_int(_nested_get(realtime, "session", "max_response_turn_sec"), default=300),
            ),
            max_concurrent_sessions=_overlay_int(
                env,
                "MONKEYBOT_REALTIME_SESSION_MAX_CONCURRENT_SESSIONS",
                _to_int(_nested_get(realtime, "session", "max_concurrent_sessions"), default=100),
            ),
        ),
        metrics=RealtimeMetricsConfig(
            emit_summary_on_close=_overlay_bool(
                env,
                "MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE",
                _to_bool(_nested_get(realtime, "metrics", "emit_summary_on_close"), default=True),
            ),
        ),
        model=RealtimeModelConfig(
            name=_nested_get(realtime, "model", "name"),
            provider=_nested_get(realtime, "model", "provider"),
        ),
    )


def get_realtime_config(config_path: str | None = None) -> RealtimeConfig:
    """Parse ``realtime.*`` from monkeybot.yaml, applying defaults."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    return realtime_config_from_doc(doc)


__all__ = [
    "RealtimeAudioConfig",
    "RealtimeConfig",
    "RealtimeMetricsConfig",
    "RealtimeModelConfig",
    "RealtimeSessionConfig",
    "RealtimeWebSocketConfig",
    "get_realtime_config",
    "realtime_config_from_doc",
]
