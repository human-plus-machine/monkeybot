"""Scrubber coverage for headers, key prefixes, project ids, and secret env values."""

from __future__ import annotations

from testkit.scrub import REDACTED, scrub


def test_redacts_headers_prefixes_and_bearer() -> None:
    raw = "\n".join(
        [
            "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz",
            'x-api-key: "abcdefghijklmnop"',
            "x-goog-api-key: AIzaSyDummyKey1234567890",
            "prefix nvapi-abcdefghijklmnopqrstuvwxyz suffix",
            "untouched sentence",
        ]
    )
    cleaned = scrub(raw)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "abcdefghijklmnop" not in cleaned
    assert "AIzaSyDummyKey1234567890" not in cleaned
    assert "nvapi-abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "untouched sentence" in cleaned
    assert cleaned.count(REDACTED) >= 4


def test_redacts_project_ids_and_long_secret_env(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "unit-gcp-project")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "unit-firebase-project")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "unit-vertex-project")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret-value-123")
    raw = (
        "project unit-gcp-project firebase unit-firebase-project "
        "vertex unit-vertex-project key ollama-secret-value-123"
    )
    cleaned = scrub(raw)
    assert "unit-gcp-project" not in cleaned
    assert "unit-firebase-project" not in cleaned
    assert "unit-vertex-project" not in cleaned
    assert "ollama-secret-value-123" not in cleaned
    assert REDACTED in cleaned


def test_redacts_lowercase_secret_env_names(monkeypatch) -> None:
    monkeypatch.setenv("ollama_api_key", "lowercase-secret-value-123")
    cleaned = scrub("key lowercase-secret-value-123 end")
    assert "lowercase-secret-value-123" not in cleaned
    assert cleaned == f"key {REDACTED} end"


def test_redacts_header_inside_a_quoted_source_line() -> None:
    cleaned = scrub('print("x-api-key: headersecretvalue123456")')
    assert "headersecretvalue123456" not in cleaned
    assert REDACTED in cleaned


def test_leaves_short_secret_env_values(monkeypatch) -> None:
    monkeypatch.setenv("UNIT_API_KEY", "1")
    monkeypatch.setenv("UNIT_TOKEN", "true")
    monkeypatch.setenv("UNIT_SECRET", "short")
    cleaned = scrub("n=1 flag true token short stays")
    assert cleaned == "n=1 flag true token short stays"
