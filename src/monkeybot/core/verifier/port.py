"""Async judge port. The loop never awaits ``verify``; a worker owns that."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from monkeybot.core.persistence.goal_ledger import ResolvedIntent
from monkeybot.core.runtime.events import VerifierVerdict


@dataclass(frozen=True)
class EvidenceBundle:
    thread_id: str
    request_id: str
    inner_turn: int
    signals: tuple[str, ...]
    intent: ResolvedIntent | None


class VerifierPort(Protocol):
    async def verify(self, intent: ResolvedIntent | None, evidence: EvidenceBundle) -> VerifierVerdict:
        """Return a verdict. Callers fail open if this raises."""
        ...


class ScriptedVerifier:
    """Test double: queued verdicts, optional delay, records calls."""

    def __init__(self, results: list[VerifierVerdict], *, delay_s: float = 0.0) -> None:
        self._results = list(results)
        self._delay_s = delay_s
        self.calls: list[EvidenceBundle] = []

    async def verify(
        self, intent: ResolvedIntent | None, evidence: EvidenceBundle
    ) -> VerifierVerdict:
        del intent
        import asyncio

        self.calls.append(evidence)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        if not self._results:
            return VerifierVerdict(
                request_id=evidence.request_id,
                verdict_id="scripted-empty",
                checkpoint_id=f"{evidence.request_id}:{evidence.inner_turn}",
                status="on_track",
                severity="none",
            )
        return self._results.pop(0)
