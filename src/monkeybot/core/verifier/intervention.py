"""Deterministic, trusted verifier interventions.

Judge/LLM free-form text is telemetry only. Anything injected into the agent
system prompt is generated here from tracker signal names the harness already
trusts.
"""

from __future__ import annotations

from collections.abc import Iterable

SIGNAL_INSTRUCTIONS: dict[str, str] = {
    "error_streak": (
        "Repeated tool failures detected. Stop retrying the same failing action "
        "and choose another approach."
    ),
    "write_without_read": (
        "A write happened without a prior read of the same path. Read the file first, then write."
    ),
    "rewrite_churn": (
        "The same file is being rewritten repeatedly. Stop churning and change approach."
    ),
    "constraint_touch": (
        "A standing constraint was touched. Stay inside the user's stated constraints."
    ),
    "repeat_correction": (
        "A previously corrected constraint was touched again. Honor the standing correction."
    ),
    "done_unmet": (
        "The stated completion condition is still unmet. Finish the remaining required work."
    ),
}

_FALLBACK = (
    "The current approach is not making progress. Change strategy before repeating the same action."
)
_REPLAN_TAIL = "Do not call tools this turn. Restate the plan."


def ordered_signals(signals: Iterable[str]) -> tuple[str, ...]:
    """De-duplicate while preserving first-seen order."""
    return tuple(dict.fromkeys(s for s in signals if s))


def correction_text(signals: Iterable[str]) -> str:
    """Trusted one-block nudge. Always starts with ``[Verifier]``."""
    known = [s for s in ordered_signals(signals) if s in SIGNAL_INSTRUCTIONS]
    body = " ".join(SIGNAL_INSTRUCTIONS[s] for s in known) if known else _FALLBACK
    return f"[Verifier] {body}"


def replan_text(signals: Iterable[str]) -> str:
    """Trusted replan note plus the one-turn no-tools instruction."""
    return f"{correction_text(signals)}\n{_REPLAN_TAIL}"
