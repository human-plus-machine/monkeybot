"""Secret scrubber for ledger records and cassette writers. Stdlib only."""

from __future__ import annotations

import os
import re

REDACTED = "[REDACTED]"

_PROJECT_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "FIREBASE_PROJECT_ID",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
_MIN_SECRET_LEN = 8
# Project ids shorter than this are left alone so a value like "a" cannot
# blank out ordinary text. Real project ids are longer.
_MIN_PROJECT_LEN = 4

_QUOTED_HEADER = re.compile(
    r"(?i)"
    r"([\"']?(?:authorization|x-goog-api-key|x-api-key)[\"']?\s*[:=]\s*)"
    r"([\"'])(.*?)(\2)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9\-._~+/]+=*)")
_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
_NVAPI = re.compile(r"\bnvapi-[A-Za-z0-9_\-]{8,}")
_BARE_HEADER = re.compile(
    r"(?i)(\b(?:authorization|x-goog-api-key|x-api-key)\b\s*[:=]\s*)([^\r\n\"']+)"
)


def scrub(text: str) -> str:
    """Redact secrets. Safe to call again on already-scrubbed text."""
    if not text:
        return text
    text = _QUOTED_HEADER.sub(_redact_quoted, text)
    text = _BEARER.sub(_redact_bearer, text)
    text = _SK.sub(REDACTED, text)
    text = _NVAPI.sub(REDACTED, text)
    text = _BARE_HEADER.sub(_redact_bare, text)
    for value in _literal_secrets():
        text = _replace_literal(text, value)
    return text


def _redact_quoted(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(4)}"


def _redact_bearer(match: re.Match[str]) -> str:
    token = match.group(2)
    if len(token) < _MIN_SECRET_LEN:
        return match.group(0)
    return f"{match.group(1)}{REDACTED}"


def _redact_bare(match: re.Match[str]) -> str:
    return f"{match.group(1)}{REDACTED}"


def _literal_secrets() -> list[str]:
    found: list[str] = []
    for name in _PROJECT_ENV:
        value = os.environ.get(name, "")
        if len(value) >= _MIN_PROJECT_LEN:
            found.append(value)
    for name, value in os.environ.items():
        if name.endswith(_SECRET_SUFFIXES) and len(value) >= _MIN_SECRET_LEN:
            found.append(value)
    # Longer values first so a short secret that is a prefix does not leave a tail.
    found.sort(key=len, reverse=True)
    unique: list[str] = []
    seen: set[str] = set()
    for value in found:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _replace_literal(text: str, value: str) -> str:
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
    return pattern.sub(REDACTED, text)
