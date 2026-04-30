"""Functional tests — pipeline-level flows using the real assembler but a stub LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness import (
    AgentSpec,
    ApprovalDenied,
    EventKind,
    HarnessConfig,
    HarnessEvent,
    IdentitySpec,
    ObservabilitySpec,
    RunPackage,
    RunPackageSpec,
    SandboxSpec,
    SecuritySpec,
    build_universal_agent,
)
from src.core.harness.principal import make_user_principal


def _mk_cfg(tmp_path: Path) -> HarnessConfig:
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "RULES.md").write_text(
        "# rules\n"
        "- [R-1] DENY_TOOL: git push\n"
    )
    return HarnessConfig(
        agent=AgentSpec(name="func-test"),
        identity=IdentitySpec(dir=str(mem), enforce_rules=True),
        security=SecuritySpec(principal_required=False),
        sandbox=SandboxSpec(backend="local_shell"),
        observability=ObservabilitySpec(run_package=RunPackageSpec(writer="local", sink_uri=str(tmp_path / "runs"))),
    )


@pytest.mark.asyncio
async def test_build_and_invoke_writes_run_package(tmp_path: Path) -> None:
    compiled = build_universal_agent(_mk_cfg(tmp_path))
    captured: list[str] = []

    class _Recorder:
        name = "rec"

        async def handle(self, event):
            captured.append(event.kind.value)

    compiled.event_bus.subscribe(_Recorder())

    result = await compiled.ainvoke(
        [{"role": "user", "content": "hello"}],
        principal=make_user_principal(user_id="alice", email="alice@example.com"),
    )
    assert result["outcome"] == "pass"
    assert EventKind.AGENT_START.value in captured
    assert EventKind.AGENT_END.value in captured

    written = list((tmp_path / "runs").rglob("*.json"))
    assert len(written) == 1
    assert result["run_id"] in written[0].name


@pytest.mark.asyncio
async def test_session_registry_registered_on_invoke(tmp_path: Path) -> None:
    compiled = build_universal_agent(_mk_cfg(tmp_path))
    result = await compiled.ainvoke(
        [{"role": "user", "content": "hi"}],
        session_id="my-session",
        principal=make_user_principal(user_id="bob"),
    )
    assert result["session_id"] == "my-session"
    report = await compiled.session_registry.introspect("my-session")
    assert report.agent_name == "func-test"
    assert report.status == "active"


@pytest.mark.asyncio
async def test_rules_enforcement_is_wired(tmp_path: Path) -> None:
    from src.core.harness.errors import RuleViolation
    from src.core.harness.middleware.rules import RulesEnforcementMW

    compiled = build_universal_agent(_mk_cfg(tmp_path))
    rules_mw = next(mw for mw in compiled.middleware if isinstance(mw, RulesEnforcementMW))
    with pytest.raises(RuleViolation):
        await rules_mw.check_tool_call("git push", {"remote": "origin"})


@pytest.mark.asyncio
async def test_hitl_wired_with_disabled_channel(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    cfg = cfg.model_copy(update={"hitl": cfg.hitl.model_copy(update={"mode": "disabled"})})
    compiled = build_universal_agent(cfg)
    decision = await compiled.approval_channel.request(
        # minimal shape: build via mw? Use the channel directly with a fake req dict via our models
        __import__("src.core.harness.hitl.protocol", fromlist=["ApprovalRequest"]).ApprovalRequest(
            approval_id="a1",
            run_id="r",
            session_id="s",
            principal=make_user_principal(user_id="u"),
            intended_action="noop",
            blast_radius="none",
            rollback_plan="none",
            confidence=0.9,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
    )
    assert decision.decision == "approved"


@pytest.mark.asyncio
async def test_run_package_records_tool_calls_from_event_bus(tmp_path: Path) -> None:
    compiled = build_universal_agent(_mk_cfg(tmp_path))

    class _Inject:
        name = "inject_tool_bus"

        def __init__(self, compiled_agent: object) -> None:
            self._compiled = compiled_agent

        async def handle(self, event: HarnessEvent) -> None:
            if event.kind != EventKind.AGENT_START:
                return
            bus = self._compiled.event_bus  # type: ignore[attr-defined]
            await bus.publish(
                HarnessEvent(
                    run_id=event.run_id,
                    session_id=event.session_id,
                    principal=event.principal,
                    versions=event.versions,
                    ts=event.ts,
                    kind=EventKind.TOOL_CALL,
                    payload={"call_id": "inj1", "name": "list_dir", "args_redacted": {}},
                )
            )
            await bus.publish(
                HarnessEvent(
                    run_id=event.run_id,
                    session_id=event.session_id,
                    principal=event.principal,
                    versions=event.versions,
                    ts=event.ts,
                    kind=EventKind.TOOL_RESULT,
                    payload={"call_id": "inj1", "result_summary": "files", "latency_ms": 2, "success": True},
                )
            )

    compiled.event_bus.subscribe(_Inject(compiled))
    await compiled.ainvoke(
        [{"role": "user", "content": "ping"}],
        principal=make_user_principal(user_id="inject-user"),
    )
    written = list((tmp_path / "runs").rglob("*.json"))
    assert written
    pkg = RunPackage.model_validate_json(written[-1].read_text(encoding="utf-8"))
    assert any(tc.name == "list_dir" for tc in pkg.tool_calls)
