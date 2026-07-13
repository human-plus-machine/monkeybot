"""Context Epoch — stable baseline + mid-conversation volatile updates.

An epoch is the span during which one rendered system-prompt baseline remains the
immutable provider-cache prefix. Volatile sources (memory, skills, current-request)
may change within an epoch and produce chronological system-context updates without
rewriting the baseline. Compaction (or an incompatible stable-source change) starts
a new epoch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal


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
    ) -> EpochAdmit:
        """Admit current stable/volatile renders at a provider-turn boundary."""
        if (
            self._force_new
            or self._state is None
            or stable_fingerprint != self._state.stable_fingerprint
        ):
            return self._open_epoch(
                stable_baseline=stable_baseline,
                volatile_text=volatile_text,
                stable_fingerprint=stable_fingerprint,
                volatile_fingerprint=volatile_fingerprint,
                changed_sources=("epoch",),
            )

        assert self._state is not None
        if volatile_fingerprint == self._state.volatile_fingerprint:
            return EpochAdmit(
                kind="unchanged",
                epoch_id=self._state.epoch_id,
                leading_system_text=self._leading(self._state),
                mid_conversation_update="",
                changed_sources=(),
            )

        # Mid-epoch volatile update: keep leading baseline byte-identical for cache.
        self._state.current_volatile = volatile_text
        self._state.volatile_fingerprint = volatile_fingerprint
        return EpochAdmit(
            kind="volatile_updated",
            epoch_id=self._state.epoch_id,
            leading_system_text=self._leading(self._state),
            mid_conversation_update=_format_system_context_update(volatile_text),
            changed_sources=("volatile",),
        )

    def _open_epoch(
        self,
        *,
        stable_baseline: str,
        volatile_text: str,
        stable_fingerprint: str,
        volatile_fingerprint: str,
        changed_sources: tuple[str, ...],
    ) -> EpochAdmit:
        next_id = 1 if self._state is None else self._state.epoch_id + 1
        self._state = _EpochState(
            epoch_id=next_id,
            stable_baseline=stable_baseline,
            stable_fingerprint=stable_fingerprint,
            baseline_volatile=volatile_text,
            volatile_fingerprint=volatile_fingerprint,
            current_volatile=volatile_text,
        )
        self._force_new = False
        return EpochAdmit(
            kind="new_epoch",
            epoch_id=next_id,
            leading_system_text=self._leading(self._state),
            mid_conversation_update="",
            changed_sources=changed_sources,
        )

    @staticmethod
    def _leading(state: _EpochState) -> str:
        return f"{state.stable_baseline}{state.baseline_volatile}"


def _format_system_context_update(volatile_text: str) -> str:
    body = volatile_text.lstrip("\n")
    if not body.strip():
        return (
            "## System context update\n"
            "Volatile context sections (memory, skills, current request) were cleared."
        )
    return (
        "## System context update\n"
        "The following replaces prior mid-epoch memory, skills, and current-request "
        "sections for this conversation.\n\n"
        f"{body}"
    )
