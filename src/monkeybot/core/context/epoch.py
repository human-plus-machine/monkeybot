"""Context Epoch — stable baseline + mid-conversation volatile updates.

An epoch is the span during which one rendered system-prompt baseline remains the
immutable provider-cache prefix. Volatile sources (memory, skills, current-request)
may change within an epoch and produce chronological system-context updates without
rewriting the baseline. Compaction (or an incompatible stable-source change) starts
a new epoch.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal


# Heading for the chronological mid-epoch update text (see
# ``_format_system_context_update``). Exported so callers that need to locate
# the stable/volatile boundary in a flattened prompt string (e.g. Anthropic
# cache-block splitting in ``providers._utils.split_system_prompt_for_cache``)
# import this instead of re-declaring the literal and risking drift.
SYSTEM_CONTEXT_UPDATE_HEADING = "\n\n## System context update\n"


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
    """Chronological update text when volatile sources changed mid-epoch; else empty."""
    mid_conversation_update: str
    changed_sources: tuple[str, ...]


@dataclass
class _EpochState:
    epoch_id: int
    stable_baseline: str
    stable_fingerprint: str
    """Volatile text admitted into the leading baseline at epoch start."""
    baseline_volatile: str
    volatile_fingerprint: str
    """Latest volatile text (may diverge from baseline_volatile after updates)."""
    current_volatile: str
    """Per-source fingerprints as of the last admitted volatile render (for

    diagnosing which specific source changed on the next reconcile — memory,
    skills, current-request — rather than reporting the opaque catch-all
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
        volatile_part_fingerprints: Mapping[str, str] | None = None,
    ) -> EpochAdmit:
        """Admit current stable/volatile renders at a provider-turn boundary.

        ``volatile_part_fingerprints`` (e.g. ``{"memory": ..., "skills": ...,
        "current_request": ...}``) is optional; when supplied it makes
        ``EpochAdmit.changed_sources`` name the specific volatile source(s) that
        changed instead of the catch-all ``"volatile"``.
        """
        admit, next_state, force_new = self._compute(
            stable_baseline=stable_baseline,
            volatile_text=volatile_text,
            stable_fingerprint=stable_fingerprint,
            volatile_fingerprint=volatile_fingerprint,
            volatile_part_fingerprints=volatile_part_fingerprints,
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
        volatile_part_fingerprints: Mapping[str, str] | None = None,
    ) -> EpochAdmit:
        """Compute the :class:`EpochAdmit` for the given renders without mutating state.

        Use for out-of-band accounting (e.g. token recounts) that must reflect the
        true wire shape — leading baseline plus any mid-conversation update — without
        advancing the epoch or admitting a volatile update that wasn't actually sent.
        """
        admit, _next_state, _force_new = self._compute(
            stable_baseline=stable_baseline,
            volatile_text=volatile_text,
            stable_fingerprint=stable_fingerprint,
            volatile_fingerprint=volatile_fingerprint,
            volatile_part_fingerprints=volatile_part_fingerprints,
        )
        return admit

    def _compute(
        self,
        *,
        stable_baseline: str,
        volatile_text: str,
        stable_fingerprint: str,
        volatile_fingerprint: str,
        volatile_part_fingerprints: Mapping[str, str] | None,
    ) -> tuple[EpochAdmit, _EpochState | None, bool]:
        parts = dict(volatile_part_fingerprints) if volatile_part_fingerprints else {}
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
                volatile_fingerprint=volatile_fingerprint,
                current_volatile=volatile_text,
                volatile_part_fingerprints=parts,
            )
            admit = EpochAdmit(
                kind="new_epoch",
                epoch_id=next_id,
                leading_system_text=self._leading(next_state),
                mid_conversation_update="",
                changed_sources=("epoch",),
            )
            return admit, next_state, False

        state = self._state
        if volatile_fingerprint == state.volatile_fingerprint:
            admit = EpochAdmit(
                kind="unchanged",
                epoch_id=state.epoch_id,
                leading_system_text=self._leading(state),
                mid_conversation_update="",
                changed_sources=(),
            )
            return admit, state, self._force_new

        # Mid-epoch volatile update: keep leading baseline byte-identical for cache.
        changed = _diff_source_names(state.volatile_part_fingerprints, parts)
        next_state = dataclasses.replace(
            state,
            current_volatile=volatile_text,
            volatile_fingerprint=volatile_fingerprint,
            volatile_part_fingerprints=parts,
        )
        admit = EpochAdmit(
            kind="volatile_updated",
            epoch_id=next_state.epoch_id,
            leading_system_text=self._leading(next_state),
            mid_conversation_update=_format_system_context_update(volatile_text),
            changed_sources=changed,
        )
        return admit, next_state, self._force_new

    @staticmethod
    def _leading(state: _EpochState) -> str:
        return f"{state.stable_baseline}{state.baseline_volatile}"


def _diff_source_names(
    prior: Mapping[str, str], current: Mapping[str, str]
) -> tuple[str, ...]:
    """Names of volatile sources whose fingerprint changed (added/removed/edited).

    Falls back to the catch-all ``"volatile"`` when the caller didn't supply
    per-source fingerprints (``current`` and ``prior`` both empty) — the
    volatile text still changed (caller already checked the whole-text
    fingerprint), we just can't attribute it to a named source.
    """
    names = sorted(set(prior) | set(current))
    changed = tuple(name for name in names if prior.get(name) != current.get(name))
    return changed or ("volatile",)


def _format_system_context_update(volatile_text: str) -> str:
    heading = SYSTEM_CONTEXT_UPDATE_HEADING.lstrip("\n")
    body = volatile_text.lstrip("\n")
    if not body.strip():
        return (
            f"{heading}"
            "Volatile context sections (memory, skills, current request) were cleared."
        )
    return (
        f"{heading}"
        "The following replaces prior mid-epoch memory, skills, and current-request "
        "sections for this conversation.\n\n"
        f"{body}"
    )
