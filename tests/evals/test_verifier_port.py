"""Stub VerifierPort pairs for deterministic loop evals (Phase 4)."""

from __future__ import annotations

import pytest

from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.verifier import EvidenceBundle, ScriptedVerifier


@pytest.mark.asyncio
async def test_scripted_verifier_returns_queued_verdict() -> None:
    queued = VerifierVerdict(
        request_id="r1",
        verdict_id="v1",
        status="drifting",
        severity="nudge",
        rationale="constraint_touch",
        triggering_signals=("constraint_touch",),
    )
    port = ScriptedVerifier([queued])
    evidence = EvidenceBundle(
        thread_id="t1",
        request_id="r1",
        inner_turn=3,
        signals=("constraint_touch",),
        intent=None,
    )
    out = await port.verify(None, evidence)
    assert out.verdict_id == "v1"
    assert port.calls == [evidence]
