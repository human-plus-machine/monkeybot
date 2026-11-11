"""Fixed character caps for prompt sections that otherwise grow without a bound.

The numbers are harness policy, like the pressure ratios in ``context_budget``.
They are character counts because a per-section token count would be a provider
round trip, and the same text must stay byte-identical for an unchanged input.
"""

from __future__ import annotations

SECTION_CHAR_CAPS: dict[str, int] = {
    "todos": 6_000,
    "background_tasks": 2_000,
    "runtime_notes": 8_000,
    "repo_overview": 4_000,
}

_RUNTIME_NOTES_MARKER = "… [runtime notes truncated]"


def clip_lines(body: str, cap: int, *, note: str) -> str:
    """Keep a prefix of whole lines, then ``note``, within ``cap`` characters.

    A first line longer than the remaining budget is cut so the note still fits.
    ``body`` is returned unchanged when it already fits.
    """
    if cap <= 0:
        return ""
    if len(body) <= cap:
        return body
    if len(note) >= cap:
        return note[:cap]
    budget = cap - len(note) - 1
    kept: list[str] = []
    used = 0
    for line in body.split("\n"):
        extra = len(line) if not kept else len(line) + 1
        if used + extra <= budget:
            kept.append(line)
            used += extra
            continue
        if not kept and budget > 0:
            kept.append(line[:budget])
        break
    if not kept:
        return note[:cap]
    return "\n".join(kept) + "\n" + note


def clip_head_and_tail(text: str, cap: int, *, marker: str = _RUNTIME_NOTES_MARKER) -> str:
    """Keep the start and the end of ``text``, with ``marker`` between them.

    The tail is what a caller joined last, so a note added at the end survives.
    ``text`` is returned unchanged when it already fits.
    """
    if cap <= 0:
        return ""
    if len(text) <= cap:
        return text
    overhead = len(marker) + 2
    if overhead >= cap:
        return text[:cap]
    remain = cap - overhead
    head_len = remain // 2
    tail_len = remain - head_len
    tail = text[-tail_len:] if tail_len else ""
    return f"{text[:head_len]}\n{marker}\n{tail}"
