"""Unit tests for Redactor / RedactionMW."""

from __future__ import annotations

from src.core.harness.middleware.redaction import RedactionMW
from src.core.harness.redaction import Redactor
from src.core.harness.specs import SecuritySpec


def test_redactor_handles_api_key_pattern() -> None:
    r = Redactor(SecuritySpec().redaction_patterns)
    text, redacted = r.redact('set api_key="AKIA12345678"')
    assert redacted
    assert "AKIA12345678" not in text
    assert "<redacted>" in text


def test_redactor_is_noop_on_clean_text() -> None:
    r = Redactor(SecuritySpec().redaction_patterns)
    text, redacted = r.redact("hello world")
    assert not redacted
    assert text == "hello world"


def test_redact_messages_preserves_structure() -> None:
    r = Redactor(SecuritySpec().redaction_patterns)
    mw = RedactionMW(r, direction="in")
    msgs = [
        {"role": "user", "content": 'password="supersecret1234"'},
        {"role": "assistant", "content": "ok"},
    ]
    out, redacted = mw.redact_messages(msgs)
    assert redacted
    assert "supersecret1234" not in out[0]["content"]
    assert out[1]["content"] == "ok"
