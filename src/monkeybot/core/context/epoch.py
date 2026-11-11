"""Context epoch — a stable system prefix plus a live tail.

An epoch is the span during which one system-prompt prefix stays byte-identical,
so a provider cache can reuse it and everything after it. The prefix is only the
stable text the caller admits. Live sections (current date, memory, the todo
list, the current request) are never folded into it: every provider call carries
them in a tail at the end of the messages, including the first call of the epoch.
A change to the stable text, or compaction, starts a new epoch.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

# Heading for the live tail (see ``_format_context_tail``), defined in the
# import-free headings leaf and re-exported here, where callers have always found it.
from monkeybot.core.prompts.headings import SYSTEM_CONTEXT_UPDATE_HEADING, heading_marker


def fingerprint_text(*parts: str) -> str:
    """Stable short hash of concatenated text parts (empty parts allowed)."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class EpochAdmit:
    """Result of reconciling system context at a safe provider-turn boundary."""

    kind: Literal["unchanged", "volatile_updated", "new_epoch"]
    epoch_id: int
    """System text for the leading system message. The stable prefix only."""
    leading_system_text: str
    """Every non-empty live section, sent at the end of the messages.

    This is the current text, not a diff against an earlier render, and it is
    sent on the first call of an epoch too. Empty when every live section is
    empty.
    """
    context_tail: str
    changed_sources: tuple[str, ...]
    """Stable sections admitted into the leading baseline, in prompt order."""
    stable_parts: dict[str, str] = field(default_factory=dict)
    """Live sections on this call. They ride the tail, not the system text."""
    leading_volatile_parts: dict[str, str] = field(default_factory=dict)


@dataclass
class _EpochState:
    epoch_id: int
    stable_baseline: str
    stable_fingerprint: str
    """Whole-text fingerprint of the last admitted live render."""
    volatile_fingerprint: str
    """Per-source fingerprints as of the last admitted volatile render (for

    diagnosing which specific source changed on the next reconcile — current date,
    memory, skills, current-request — rather than reporting the opaque catch-all
    ``"volatile"``.
    """
    volatile_part_fingerprints: dict[str, str] = field(default_factory=dict)


class ContextEpochTracker:
    """Track one user message's context epoch across inner provider calls.

    Call :meth:`reconcile` at each safe provider-turn boundary. Call
    :meth:`begin_new_epoch` after compaction so the next reconcile admits the
    current stable text as a fresh prefix. Live sections stay in the tail.
    """

    def __init__(self) -> None:
        self._state: _EpochState | None = None
        self._force_new = True

    def begin_new_epoch(self) -> None:
        """Mark that the next reconcile must open a new epoch (post-compaction)."""
        self._force_new = True

    def reconcile(
        self,
        *,
        stable_baseline: str,
        volatile_text: str,
        stable_fingerprint: str,
        volatile_fingerprint: str,
        volatile_parts: Mapping[str, str] | None = None,
        volatile_part_fingerprints: Mapping[str, str] | None = None,
        stable_parts: Mapping[str, str] | None = None,
    ) -> EpochAdmit:
        """Admit current stable/volatile renders at a provider-turn boundary.

        ``volatile_parts`` (e.g. ``{"current_date": ..., "memory": ..., "todos": ...,
        "current_request": ...}``) is the per-section live text. The tail is every
        section that has text, on every reconcile, including the one that opens the
        epoch. ``volatile_part_fingerprints`` is optional; when supplied it makes
        ``EpochAdmit.changed_sources`` name the specific source(s) that changed
        since the last reconcile instead of the catch-all ``"volatile"``. When
        omitted and ``volatile_parts`` is given, the fingerprints are derived from
        the section text.
        """
        admit, next_state, force_new = self._compute(
            stable_baseline=stable_baseline,
            volatile_text=volatile_text,
            stable_fingerprint=stable_fingerprint,
            volatile_fingerprint=volatile_fingerprint,
            volatile_parts=volatile_parts,
            volatile_part_fingerprints=volatile_part_fingerprints,
            stable_parts=stable_parts,
        )
        self._state = next_state
        self._force_new = force_new
        return admit

    def peek(
        self,
        *,
        stable_baseline: str,
        volatile_text: str,
        stable_fingerprint: str,
        volatile_fingerprint: str,
        volatile_parts: Mapping[str, str] | None = None,
        volatile_part_fingerprints: Mapping[str, str] | None = None,
        stable_parts: Mapping[str, str] | None = None,
    ) -> EpochAdmit:
        """Compute the :class:`EpochAdmit` for the given renders without mutating state.

        Use for out-of-band accounting (e.g. token recounts) that must reflect the
        true wire shape — leading baseline plus the live tail — without advancing
        the epoch or admitting a volatile update that wasn't actually sent.
        """
        admit, _next_state, _force_new = self._compute(
            stable_baseline=stable_baseline,
            volatile_text=volatile_text,
            stable_fingerprint=stable_fingerprint,
            volatile_fingerprint=volatile_fingerprint,
            volatile_parts=volatile_parts,
            volatile_part_fingerprints=volatile_part_fingerprints,
            stable_parts=stable_parts,
        )
        return admit

    def _compute(
        self,
        *,
        stable_baseline: str,
        volatile_text: str,
        stable_fingerprint: str,
        volatile_fingerprint: str,
        volatile_parts: Mapping[str, str] | None,
        volatile_part_fingerprints: Mapping[str, str] | None,
        stable_parts: Mapping[str, str] | None = None,
    ) -> tuple[EpochAdmit, _EpochState | None, bool]:
        parts = _part_fingerprints(volatile_parts, volatile_part_fingerprints)
        current_parts = dict(volatile_parts) if volatile_parts is not None else {}
        admitted_stable = dict(stable_parts or {})
        tail = _live_tail(volatile_text, volatile_parts)
        if (
            self._force_new
            or self._state is None
            or stable_fingerprint != self._state.stable_fingerprint
        ):
            next_id = 1 if self._state is None else self._state.epoch_id + 1
            next_state = _EpochState(
                epoch_id=next_id,
                stable_baseline=stable_baseline,
                stable_fingerprint=stable_fingerprint,
                volatile_fingerprint=volatile_fingerprint,
                volatile_part_fingerprints=parts,
            )
            admit = EpochAdmit(
                kind="new_epoch",
                epoch_id=next_id,
                leading_system_text=stable_baseline,
                context_tail=tail,
                changed_sources=("epoch",),
                stable_parts=admitted_stable,
                leading_volatile_parts=current_parts,
            )
            return admit, next_state, False

        state = self._state
        if volatile_fingerprint == state.volatile_fingerprint:
            admit = EpochAdmit(
                kind="unchanged",
                epoch_id=state.epoch_id,
                leading_system_text=state.stable_baseline,
                context_tail=tail,
                changed_sources=(),
                stable_parts=admitted_stable,
                leading_volatile_parts=current_parts,
            )
            return admit, state, self._force_new

        # The live sections moved. The system prefix stays the text admitted at
        # epoch start; only the last-seen fingerprints move, so
        # ``changed_sources`` reports this reconcile's delta. The tail is the
        # full current render, not the sections that moved.
        changed = _diff_source_names(state.volatile_part_fingerprints, parts)
        next_state = dataclasses.replace(
            state,
            volatile_fingerprint=volatile_fingerprint,
            volatile_part_fingerprints=parts,
        )
        admit = EpochAdmit(
            kind="volatile_updated",
            epoch_id=next_state.epoch_id,
            leading_system_text=next_state.stable_baseline,
            context_tail=tail,
            changed_sources=changed,
            stable_parts=admitted_stable,
            leading_volatile_parts=current_parts,
        )
        return admit, next_state, self._force_new


def _part_fingerprints(
    volatile_parts: Mapping[str, str] | None,
    volatile_part_fingerprints: Mapping[str, str] | None,
) -> dict[str, str]:
    if volatile_part_fingerprints:
        return dict(volatile_part_fingerprints)
    if volatile_parts is not None:
        return {name: fingerprint_text(text) for name, text in volatile_parts.items()}
    return {}


def _live_tail(volatile_text: str, current_parts: Mapping[str, str] | None) -> str:
    """The update heading plus every live section that has text.

    A caller that passes sections gets those sections. A caller that passes only
    the whole render gets that render. Either way an empty render sends no tail,
    and nothing is described as replacing or clearing an earlier copy: the tail
    is the only copy the model is shown.
    """
    if current_parts is not None:
        bodies = [text.lstrip("\n") for text in current_parts.values() if text.strip()]
    else:
        body = volatile_text.lstrip("\n")
        bodies = [body] if body.strip() else []
    if not bodies:
        return ""
    heading = heading_marker(SYSTEM_CONTEXT_UPDATE_HEADING).strip()
    return heading + "\n\n" + "\n\n".join(bodies)


def _diff_source_names(prior: Mapping[str, str], current: Mapping[str, str]) -> tuple[str, ...]:
    """Names of volatile sources whose fingerprint changed (added/removed/edited).

    Falls back to the catch-all ``"volatile"`` when the caller didn't supply
    per-source fingerprints (``current`` and ``prior`` both empty) — the
    volatile text still changed (caller already checked the whole-text
    fingerprint), we just can't attribute it to a named source.
    """
    names = sorted(set(prior) | set(current))
    changed = tuple(name for name in names if prior.get(name) != current.get(name))
    return changed or ("volatile",)
