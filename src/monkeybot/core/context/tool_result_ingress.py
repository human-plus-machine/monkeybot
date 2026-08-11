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
# Only applies to denylisted blob keys (data/base64/…), not arbitrary text fields
# like browser ``tree``. Whole-result size is handled by spill + derived caps.
_BLOB_JSON_FIELD_MAX_CHARS = 512
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
# `search` snippets are plain text pulled from indexed workspace/note
# files (never raw binary/base64 blobs — see extractors.is_probably_text),
# and evidence_guard.py matches cited Evidence paths against exact source
# text, so these must round-trip byte-for-byte like read_file/glob.
SANITIZE_SKIP_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "replace_in_file",
        "glob",
        "search_memory",
        "search",
        "list_skills",
    }
)


def skip_tool_result_sanitize(tool_name: str) -> bool:
    """True when tool results must not pass through ``sanitize_tool_result_text``."""
    return tool_name in SANITIZE_SKIP_TOOL_NAMES


def sanitize_enabled_from_env() -> bool:
    raw = os.environ.get("MONKEYBOT_TOOL_RESULT_SANITIZE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def blob_json_field_max_chars() -> int:
    """Max length for denylisted JSON blob fields before omission (fixed constant)."""
    return _BLOB_JSON_FIELD_MAX_CHARS


def _has_nontrivial_b64_markers(sample: str) -> bool:
    """True when ``+/`` density looks like base64, not a lone slash or plus."""
    plus_slash = sum(1 for ch in sample if ch in "+/")
    return plus_slash >= 3 or (plus_slash / max(1, len(sample)) >= 0.02)


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
    if stripped.rstrip().endswith("="):
        return True
    if _has_nontrivial_b64_markers(sample):
        return True
    return len(set(sample)) >= 6


def _is_plausible_base64_run(text: str) -> bool:
    """True for long runs that look like real base64 (not diffs / prose).

    After the alphabet-ratio gate, unpadded alphanumeric-only base64 is still
    accepted — padding / ``+/`` markers are sufficient but not required.
    """
    if len(text) < _MIN_BASE64_RUN:
        return False
    sample = text[:4096]
    if len(set(sample.strip("="))) <= 2:
        return False
    allowed = sum(1 for ch in sample if ch.isalnum() or ch in "+/=\n\r")
    if allowed / max(1, len(sample)) < 0.98:
        return False
    return len(set(sample)) >= 8


def _is_compact_non_text_blob(value: str) -> bool:
    """Dense payloads without whitespace/punctuation — omit these by size.

    Readable text (unified diffs, logs, JSON) has whitespace or punctuation
    outside the base64 alphabet; those must survive sanitize so soft spill can
    handle size. Compact ``"x" * N`` / binary-looking runs still redact.
    """
    sample = value[:4096]
    if not sample:
        return False
    if any(ch.isspace() for ch in sample):
        return False
    return all(ch.isalnum() or ch in "+/=" for ch in sample)


def _replace_long_b64(match: re.Match[str]) -> str:
    run = match.group(0)
    if not _is_plausible_base64_run(run):
        return run
    return f"[omitted ~{len(run)} char base64 run]"


def _should_redact_blob_field(key: str, value: str, *, max_field: int) -> bool:
    """True for denylisted keys that look like binary/base64 or compact blobs.

    Oversized plain text under denylisted keys (e.g. MCP ``data`` holding a
    unified diff) is left intact — whole-result size is handled by soft spill.
    """
    if key.lower() not in _REDACT_JSON_KEYS:
        return False
    if _looks_like_base64(value):
        return True
    if max_field > 0 and len(value) > max_field:
        return _is_compact_non_text_blob(value)
    return False


def _redact_json_value(key: str, value: Any, *, max_field: int) -> Any:
    if isinstance(value, str):
        if _should_redact_blob_field(key, value, max_field=max_field):
            return f"[omitted {len(value)} chars from {key!r}]"
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
    """Strip embedded binary/base64 payloads from tool result strings.

    Ordinary long text fields (e.g. browser ``tree``) are left intact; size is
    controlled later by soft spill and derived char budgets. Only denylisted blob
    keys are field-redacted here.
    """
    if not text:
        return text
    if enabled is None:
        enabled = sanitize_enabled_from_env()
    if not enabled:
        return text

    max_field = blob_json_field_max_chars()
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
    """Apply the inline history hard ceiling after sanitization/spill.

    Callers should pass a window-derived ``max_chars``. When omitted, falls back
    to the historic 32k floor (for ``load_file`` / error paths).
    """
    limit = _DEFAULT_TOOL_RESULT_MAX_CHARS if max_chars is None else max_chars
    return truncate_with_note(text, limit, label="tool result")


def summarize_tool_result_text(
    text: str,
    *,
    window_tokens: int | None = None,
    max_chars: int | None = None,
) -> str:
    """Sanitize and cap text fed into context summarization.

    Prefer passing ``window_tokens`` so the cap scales with the model window.
    Without it, falls back to the historic fixed cap.
    """
    if max_chars is None:
        if window_tokens is None:
            max_chars = _DEFAULT_SUMMARY_TOOL_RESULT_MAX_CHARS
        else:
            from monkeybot.core.tools.spill_inventory import spill_budgets_from_window

            max_chars = spill_budgets_from_window(window_tokens).summary_max_chars
    cleaned = sanitize_tool_result_text(text)
    return truncate_with_note(cleaned, max_chars, label="summarized tool result")


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
    "blob_json_field_max_chars",
    "cap_tool_result_text",
    "dump_model_or_str",
    "format_mcp_binary_block",
    "format_mcp_content_block",
    "sanitize_enabled_from_env",
    "sanitize_tool_result_text",
    "skip_tool_result_sanitize",
    "summarize_tool_result_text",
    "truncate_with_note",
]
