"""VerifierInspector read_only gate and request-scoped block expiry."""

from __future__ import annotations

from dataclasses import replace

import pytest

from monkeybot.core.config.settings import VerifierConfig, VerifierEscalationConfig
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.tools.inspector import InspectorToolCall
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.inspector import VerifierInspector
from monkeybot.core.verifier.mailbox import VerdictMailbox
from tests.core.test_loop import _ctx as loop_ctx


def _block(request_id: str = "r1") -> VerifierVerdict:
    return VerifierVerdict(
        request_id=request_id,
        verdict_id="v1",
        checkpoint_id=f"{request_id}:1",
        status="drifting",
        severity="block",
        rationale="constraint_touch",
        correction="leave the migrations alone",
    )


def _cfg() -> object:
    class Cfg:
        env_values: dict[str, str] = {}
        verifier = VerifierConfig(escalation=VerifierEscalationConfig(max_severity="block"))

    return Cfg()


@pytest.mark.asyncio
async def test_block_expires_when_request_id_changes() -> None:
    mailbox = VerdictMailbox()
    mailbox.put("t1", _block("old"))
    inspector = VerifierInspector(mailbox)
    ctx = replace(
        loop_ctx(),
        request_id="new",
        verdict_mailbox=mailbox,
        config=_cfg(),  # type: ignore[arg-type]
        tools=[ToolDef("run_command", "Run shell", {})],
    )
    decision = await inspector.check(
        InspectorToolCall(call_id="c1", name="run_command", args={}),
        ctx,
    )
    assert decision.kind == "allow"


@pytest.mark.asyncio
async def test_block_denies_parallel_safe_mutating_tool() -> None:
    mailbox = VerdictMailbox()
    mailbox.put("t1", _block("r1"))
    inspector = VerifierInspector(mailbox)
    ctx = replace(
        loop_ctx(),
        verdict_mailbox=mailbox,
        config=_cfg(),  # type: ignore[arg-type]
        tools=[ToolDef("write_file", "Write", {}, parallel_safe=True)],
    )
    decision = await inspector.check(
        InspectorToolCall(call_id="c1", name="write_file", args={"path": "x"}),
        ctx,
    )
    assert decision.kind == "deny"


@pytest.mark.asyncio
async def test_block_allows_explicit_read_only_tool() -> None:
    mailbox = VerdictMailbox()
    mailbox.put("t1", _block("r1"))
    inspector = VerifierInspector(mailbox)
    ctx = replace(
        loop_ctx(),
        verdict_mailbox=mailbox,
        config=_cfg(),  # type: ignore[arg-type]
        tools=[ToolDef("read_file", "Read", {}, parallel_safe=True, read_only=True)],
    )
    decision = await inspector.check(
        InspectorToolCall(call_id="c1", name="read_file", args={"path": "x"}),
        ctx,
    )
    assert decision.kind == "allow"
