"""Sanitize, cap, and redact tool/MCP payloads before they enter chat history."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_RESULT_MAX_CHARS = 32_768
_DEFAULT_SUMMARY_TOOL_RESULT_MAX_CHARS = 8_192
_DEFAULT_JSON_FIELD_MAX_CHARS = 512
_MIN_BASE64_RUN = 800

_DATA_URL_RE = re.compile(
    r"data:(?:image|audio|video|application)/[\w.+-]+;base64,[\sA-Za-z0-9+/=]{200,}",
    re.IGNORECASE,
)
_LONG_B64_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])(?:[A-Za-z0-9+/]{4}){200,}(?:[A-Za-z0-9+/]{0,3}={0,3})(?![A-Za-z0-9+/=])"
)

_REDACT_JSON_KEYS = frozenset(
    {
        "data",
        "base64",
        "image_data",
        "content_base64",
        "blob",
        "binary",
        "image",
        "screenshot",
        "audio",
        "bytes",
        "payload",
    }
)

# Workspace tools return faithful text/JSON; skip ingress JSON field redaction (see CoreToolExecutor).
SANITIZE_SKIP_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "replace_in_file",
        "glob",
        "search_memory",
        "list_skills",
    }
)


def skip_tool_result_sanitize(tool_name: str) -> bool:
    """True when tool results must not pass through ``sanitize_tool_result_text``."""
    return tool_name in SANITIZE_SKIP_TOOL_NAMES


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("invalid env var %s=%r; using default %s", name, raw, default)
        return default


def sanitize_enabled_from_env() -> bool:
    raw = os.environ.get("MONKEYBOT_TOOL_RESULT_SANITIZE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def tool_result_max_chars_from_env() -> int:
    return _int_from_env("MONKEYBOT_TOOL_RESULT_MAX_CHARS", _DEFAULT_TOOL_RESULT_MAX_CHARS)


def summary_tool_result_max_chars_from_env() -> int:
    return _int_from_env(
        "MONKEYBOT_SUMMARY_TOOL_RESULT_MAX_CHARS",
        _DEFAULT_SUMMARY_TOOL_RESULT_MAX_CHARS,
    )


def json_field_max_chars_from_env() -> int:
    return _int_from_env("MONKEYBOT_TOOL_RESULT_JSON_FIELD_MAX", _DEFAULT_JSON_FIELD_MAX_CHARS)


def _looks_like_base64(value: str) -> bool:
    """Return True when *value* is likely base64, including short JSON field payloads."""
    if not value:
        return False
    if _is_plausible_base64_run(value):
        return True
    stripped = value.strip()
    if len(stripped) < 16:
        return False
    if any(
        ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r"
        for ch in stripped
    ):
        return False
    sample = stripped[:512]
    if len(set(sample.strip("="))) <= 2:
        return False
    if stripped.rstrip().endswith("=") or any(ch in "+/" for ch in sample):
        return True
    return len(set(sample)) >= 6


def _is_plausible_base64_run(text: str) -> bool:
    if len(text) < _MIN_BASE64_RUN:
        return False
    sample = text[:4096]
    if len(set(sample.strip("="))) <= 2:
        return False
    if any(ch in "+/" for ch in sample):
        return True
    if sample.rstrip().endswith("="):
        return True
    allowed = sum(1 for ch in sample if ch.isalnum() or ch in "+/=\n\r")
    return allowed / max(1, len(sample)) >= 0.98 and len(set(sample)) >= 8


def _replace_long_b64(match: re.Match[str]) -> str:
    run = match.group(0)
    if not _is_plausible_base64_run(run):
        return run
    return f"[omitted ~{len(run)} char base64 run]"


def _redact_long_string(key: str, value: str, *, max_field: int) -> str:
    if len(value) <= max_field and not (
        key.lower() in _REDACT_JSON_KEYS and _looks_like_base64(value)
    ):
        return value
    return f"[omitted {len(value)} chars from {key!r}]"


def _redact_json_value(key: str, value: Any, *, max_field: int) -> Any:
    if isinstance(value, str):
        if key.lower() in _REDACT_JSON_KEYS or len(value) > max_field:
            return _redact_long_string(key, value, max_field=max_field)
        return value
    if isinstance(value, dict):
        return {k: _redact_json_value(str(k), v, max_field=max_field) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(key, item, max_field=max_field) for item in value]
    return value


def _redact_json_text(text: str, *, max_field: int) -> str:
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    redacted = _redact_json_value("", parsed, max_field=max_field)
    try:
        return json.dumps(redacted, ensure_ascii=False, default=str)
    except TypeError:
        return text


def sanitize_tool_result_text(text: str, *, enabled: bool | None = None) -> str:
    """Strip embedded binary/base64 payloads from tool result strings."""
    if not text:
        return text
    if enabled is None:
        enabled = sanitize_enabled_from_env()
    if not enabled:
        return text

    max_field = json_field_max_chars_from_env()
    out = _DATA_URL_RE.sub(
        lambda m: f"data:...;base64,[omitted {len(m.group(0))} chars]",
        text,
    )
    out = _LONG_B64_RE.sub(_replace_long_b64, out)
    return _redact_json_text(out, max_field=max_field)


def truncate_with_note(text: str, max_chars: int, *, label: str = "tool result") -> str:
    """Hard-cap ``text`` with a trailing omission note."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n[{label} truncated — {omitted} chars omitted]"


def cap_tool_result_text(text: str, *, max_chars: int | None = None) -> str:
    """Apply the inline history hard ceiling after sanitization/spill."""
    limit = tool_result_max_chars_from_env() if max_chars is None else max_chars
    return truncate_with_note(text, limit, label="tool result")


def summarize_tool_result_text(text: str, *, max_chars: int | None = None) -> str:
    """Sanitize and cap text fed into context summarization."""
    limit = summary_tool_result_max_chars_from_env() if max_chars is None else max_chars
    cleaned = sanitize_tool_result_text(text)
    return truncate_with_note(cleaned, limit, label="summarized tool result")


def format_mcp_binary_block(
    *,
    kind: str,
    data: object,
    mime: str | None,
) -> str:
    """Placeholder for MCP image/audio/resource blocks."""
    size = len(str(data or ""))
    mime_label = mime or f"{kind}/*"
    return (
        f"[{kind} omitted from tool text: mime={mime_label}, "
        f"~{size} base64 chars — use a vision-capable provider or a text-returning tool]"
    )


def dump_model_or_str(obj: Any) -> str:
    """JSON-dump ``obj.model_dump()`` when available and serializable, else ``str(obj)``."""
    md = getattr(obj, "model_dump", None)
    if callable(md):
        try:
            dumped = md(mode="python", by_alias=True)
            return json.dumps(dumped, default=str)
        except TypeError as exc:
            logger.debug("model_dump not JSON-serializable, falling back to str(): %s", exc)
    return str(obj)


def format_mcp_content_block(block: Any, *, sanitize: bool = True) -> str:
    """Turn one MCP content block into a safe string."""
    blk_type = getattr(block, "type", None)
    if blk_type == "text":
        txt = getattr(block, "text", None)
        if txt is None:
            return ""
        text = str(txt)
        return sanitize_tool_result_text(text) if sanitize else text
    if blk_type == "image":
        data = getattr(block, "data", None) or ""
        mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", None) or "image/png"
        return format_mcp_binary_block(kind="image", data=data, mime=str(mime))
    if blk_type == "audio":
        data = getattr(block, "data", None) or ""
        mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", None) or "audio/*"
        return format_mcp_binary_block(kind="audio", data=data, mime=str(mime))
    if blk_type == "resource":
        uri = getattr(block, "uri", None) or getattr(block, "resource", None) or ""
        mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", None) or "resource"
        return f"[resource omitted from tool text: uri={uri!s}, mime={mime}]"

    text = dump_model_or_str(block)
    return sanitize_tool_result_text(text) if sanitize else text


__all__ = [
    "SANITIZE_SKIP_TOOL_NAMES",
    "cap_tool_result_text",
    "dump_model_or_str",
    "format_mcp_binary_block",
    "format_mcp_content_block",
    "sanitize_enabled_from_env",
    "sanitize_tool_result_text",
    "skip_tool_result_sanitize",
    "summarize_tool_result_text",
    "summary_tool_result_max_chars_from_env",
    "tool_result_max_chars_from_env",
    "truncate_with_note",
]
