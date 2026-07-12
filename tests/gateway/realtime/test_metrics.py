"""Tests for realtime session metrics."""

from __future__ import annotations

import time

import pytest

from monkeybot.gateway.realtime.metrics import RealtimeMetrics


@pytest.fixture
def metrics() -> RealtimeMetrics:
    return RealtimeMetrics(session_id="s1", request_id="r1")


def test_initial_metrics(metrics: RealtimeMetrics) -> None:
    assert metrics.session_id == "s1"
    assert metrics.request_id == "r1"
    assert metrics.user_audio_sec == 0.0
    assert metrics.model_audio_sec == 0.0
    assert metrics.interrupt_count == 0
    assert metrics.turn_count == 0


def test_mark_audio_and_interrupt(metrics: RealtimeMetrics) -> None:
    metrics.mark_user_audio_sec(1.5)
    metrics.mark_model_audio_sec(2.5)
    metrics.mark_interrupt()
    assert metrics.user_audio_sec == 1.5
    assert metrics.model_audio_sec == 2.5
    assert metrics.interrupt_count == 1


def test_turn_metrics(metrics: RealtimeMetrics) -> None:
    start = time.monotonic()
    metrics.start_turn(start)
    metrics.mark_first_output(start + 0.1)
    metrics.mark_tool_in_turn()
    metrics.end_turn(start + 1.0)
    assert metrics.turn_count == 1
    assert metrics.turn_latencies_ms == [100.0]
    assert metrics.turn_model_durations_ms == [900.0]
    assert metrics.turn_tool_counts == [1]


def test_turn_without_first_output(metrics: RealtimeMetrics) -> None:
    metrics.start_turn()
    metrics.end_turn()
    assert metrics.turn_count == 1
    assert metrics.turn_latencies_ms == []
    assert metrics.turn_model_durations_ms == []
    assert metrics.turn_tool_counts == [0]


def test_mark_usage(metrics: RealtimeMetrics) -> None:
    metrics.mark_usage(input_tokens=10, output_tokens=20)
    metrics.mark_usage(input_tokens=5, output_tokens=0)
    assert metrics.input_tokens == 15
    assert metrics.output_tokens == 20
    assert metrics.last_prompt_tokens == 5
    summary = metrics.as_dict()
    assert summary["realtime_session_input_tokens"] == 15
    assert summary["realtime_session_output_tokens"] == 20


def test_to_usage_payload_uses_last_prompt_not_cumulative(metrics: RealtimeMetrics) -> None:
    metrics.mark_usage(input_tokens=1_000, output_tokens=50)
    metrics.mark_usage(input_tokens=2_500, output_tokens=80)
    payload = metrics.to_usage_payload(context_window_tokens=200_000)
    assert payload["input_tokens"] == 3_500
    assert payload["output_tokens"] == 130
    assert payload["last_prompt_tokens"] == 2_500
    assert payload["estimated_prompt_tokens"] == 2_500
    assert payload["context_window_tokens"] == 200_000
    assert payload["summarization_threshold_tokens"] == 170_000


def test_to_usage_payload_default_context_window(metrics: RealtimeMetrics) -> None:
    payload = metrics.to_usage_payload()
    assert payload["context_window_tokens"] == 200_000


def test_close_and_summary(metrics: RealtimeMetrics) -> None:
    metrics.mark_user_audio_sec(1.0)
    metrics.mark_interrupt()
    metrics.close("idle_timeout")
    summary = metrics.as_dict()
    assert summary["realtime_session_user_audio_sec"] == 1.0
    assert summary["realtime_session_interrupt_count"] == 1
    assert summary["realtime_session_close_reason"] == "idle_timeout"
    assert summary["realtime_session_duration_sec"] >= 0.0
