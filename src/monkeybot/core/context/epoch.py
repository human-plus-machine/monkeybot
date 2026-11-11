"""Context Epoch — stable baseline + a live volatile tail.

An epoch is the span during which one rendered system-prompt baseline remains the
immutable provider-cache prefix. Volatile sources (current date, memory, skills,
todo list, current-request) may change within an epoch. Each provider call then
carries a tail of the sections that differ from the baseline, so the model keeps
seeing them and earlier messages stay byte-identical. Compaction (or an
incompatible stable-source change) starts a new epoch and folds the current
volatile text into a fresh baseline.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

# Heading for the live tail (see ``_format_context_tail``), defined in the
# import-free headings leaf and re-exported here, where callers have always found it.
from monkeybot.core.prompts.headings import SYSTEM_CONTEXT_UPDATE_HEADING


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
    """Full system text for the leading system message (baseline within the epoch)."""
    leading_system_text: str
    """Sections that differ from the epoch baseline, sent on every later call.

    Empty when the current volatile render matches the baseline (a fresh epoch,
    or state that has returned to it).
    """
    context_tail: str
    changed_sources: tuple[str, ...]
    """Stable sections admitted into the leading baseline, in prompt order."""
    stable_parts: dict[str, str] = field(default_factory=dict)
    """Volatile sections admitted into the leading baseline. Not the live render."""
    leading_volatile_parts: dict[str, str] = field(default_factory=dict)


@dataclass
class _EpochState:
    epoch_id: int
    stable_baseline: str
    stable_fingerprint: str
    """Volatile text admitted into the leading baseline at epoch start."""
    baseline_volatile: str
    """Per-section volatile text admitted into the baseline. Not updated mid-epoch."""
    baseline_parts: dict[str, str]
    """Whole-text fingerprint of the last admitted volatile render."""
    volatile_fingerprint: str
    """Per-source fingerprints as of the last admitted volatile render (for

    diagnosing which specific source changed on the next reconcile — current date,
    memory, skills, current-request — rather than reporting the opaque catch-all
    ``"volatile"``.
    """
    volatile_part_fingerprints: dict[str, str] = field(default_factory=dict)


class ContextEpochTracker:
    """Track one session turn's context epoch across inner provider calls.

    Call :meth:`reconcile` at each safe provider-turn boundary. Call
    :meth:`begin_new_epoch` after compaction (or session move) so the next
    reconcile folds current context into a fresh baseline.
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
        "current_request": ...}``) is the per-section text. The tail is every section
        whose text differs from the text admitted at epoch start, on every reconcile
        until it matches again. ``volatile_part_fingerprints`` is optional; when
        supplied it makes ``EpochAdmit.changed_sources`` name the specific source(s)
        that changed since the last reconcile instead of the catch-all ``"volatile"``.
        When omitted and ``volatile_parts`` is given, the fingerprints are derived
        from the section text.
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
        current_parts = dict(volatile_parts) if volatile_parts is not None else None
        admitted_stable = dict(stable_parts or {})
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
                baseline_volatile=volatile_text,
                baseline_parts=dict(current_parts or {}),
                volatile_fingerprint=volatile_fingerprint,
                volatile_part_fingerprints=parts,
            )
            admit = EpochAdmit(
                kind="new_epoch",
                epoch_id=next_id,
                leading_system_text=self._leading(next_state),
                context_tail="",
                changed_sources=("epoch",),
                stable_parts=admitted_stable,
                leading_volatile_parts=dict(next_state.baseline_parts),
            )
            return admit, next_state, False

        state = self._state
        tail = _context_tail(state, volatile_text, current_parts)
        if volatile_fingerprint == state.volatile_fingerprint:
            admit = EpochAdmit(
                kind="unchanged",
                epoch_id=state.epoch_id,
                leading_system_text=self._leading(state),
                context_tail=tail,
                changed_sources=(),
                stable_parts=admitted_stable,
                leading_volatile_parts=dict(state.baseline_parts),
            )
            return admit, state, self._force_new

        # Mid-epoch volatile change: keep the leading baseline byte-identical.
        # The baseline parts stay as admitted at epoch start; only the
        # last-seen fingerprints move, so ``changed_sources`` reports this
        # reconcile's delta while the tail keeps every section still off baseline.
        changed = _diff_source_names(state.volatile_part_fingerprints, parts)
        next_state = dataclasses.replace(
            state,
            volatile_fingerprint=volatile_fingerprint,
            volatile_part_fingerprints=parts,
        )
        admit = EpochAdmit(
            kind="volatile_updated",
            epoch_id=next_state.epoch_id,
            leading_system_text=self._leading(next_state),
            context_tail=tail,
            changed_sources=changed,
            stable_parts=admitted_stable,
            leading_volatile_parts=dict(state.baseline_parts),
        )
        return admit, next_state, self._force_new

    @staticmethod
    def _leading(state: _EpochState) -> str:
        return f"{state.stable_baseline}{state.baseline_volatile}"


def _part_fingerprints(
    volatile_parts: Mapping[str, str] | None,
    volatile_part_fingerprints: Mapping[str, str] | None,
) -> dict[str, str]:
    if volatile_part_fingerprints:
        return dict(volatile_part_fingerprints)
    if volatile_parts is not None:
        return {name: fingerprint_text(text) for name, text in volatile_parts.items()}
    return {}


def _context_tail(
    state: _EpochState,
    volatile_text: str,
    current_parts: Mapping[str, str] | None,
) -> str:
    """Sections that differ from the epoch baseline, or empty when none do."""
    if current_parts is not None:
        return _format_context_tail(state.baseline_parts, current_parts)
    if volatile_text == state.baseline_volatile:
        return ""
    return _format_whole_volatile_tail(volatile_text)


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


def _format_context_tail(
    baseline_parts: Mapping[str, str],
    current_parts: Mapping[str, str],
) -> str:
    """Tail text for the sections whose text differs from the epoch baseline.

    A section that is non-empty now is included in full. A section that was
    non-empty in the baseline and is now empty is named as cleared. A section
    that matches the baseline is omitted, so returning to the baseline drops it.
    """
    order = list(current_parts)
    for name in baseline_parts:
        if name not in current_parts:
            order.append(name)
    replaced: list[str] = []
    cleared: list[str] = []
    bodies: list[str] = []
    for name in order:
        before = baseline_parts.get(name, "")
        after = current_parts.get(name, "")
        if before == after:
            continue
        label = name.replace("_", "-")
        if after.strip():
            replaced.append(label)
            bodies.append(after.lstrip("\n"))
        elif before.strip():
            cleared.append(label)
    if not replaced and not cleared:
        return ""
    return _tail_text(replaced, cleared, bodies)


def _format_whole_volatile_tail(volatile_text: str) -> str:
    """Tail for a caller that passed the volatile render but not its sections."""
    body = volatile_text.lstrip("\n")
    if not body.strip():
        return _tail_text([], ["volatile"], [])
    return _tail_text(["volatile"], [], [body])


def _tail_text(replaced: list[str], cleared: list[str], bodies: list[str]) -> str:
    heading = SYSTEM_CONTEXT_UPDATE_HEADING.lstrip("\n")
    lines = [heading.rstrip("\n")]
    if replaced:
        noun = "section" if len(replaced) == 1 else "sections"
        lines.append(
            f"The following replaces the prior {_join_names(replaced)} {noun} "
            "for this conversation."
        )
    if cleared:
        lines.append(f"Cleared: {', '.join(cleared)}.")
    text = "\n".join(lines)
    if bodies:
        text += "\n\n" + "\n\n".join(bodies)
    return text


def _join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"
