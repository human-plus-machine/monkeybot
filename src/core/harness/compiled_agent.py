"""CompiledAgent — the object returned by ``build_universal_agent`` (Agent Harness).

Exposes a small, stable surface: invoke / stream / middleware_names.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .control import SessionRegistry
from .event_bus import EventBus
from .events import EventKind, HarnessEvent, Principal, VersionTriple

# isort: off
# BEGIN harness-extensibility story 5
from .extensions.base import IdentitySource
from .extensions.values import LoadedIdentity

# END harness-extensibility story 5
# BEGIN harness-extensibility story 6
from .extensions.base import SecretResolver

# END harness-extensibility story 6
# BEGIN harness-extensibility story 7
from .extensions.base import ModelProvider

# END harness-extensibility story 7
# BEGIN harness-extensibility phase 6
from .extensions.base import Checkpointer as ExtensionsCheckpointer
from .extensions.base import JobStorage, MemoryStore

# END harness-extensibility phase 6
# isort: on
from .hitl.protocol import ApprovalChannel
from .middleware.principal_propagation import PrincipalPropagationMW
from .run_package_accumulator import (
    RunPackageAccumulator,
    SubagentInvocationHooks,
    reset_active_subagent_hooks,
    set_active_subagent_hooks,
)
from .runpackage import RunPackage
from .runpackage_writers import RunPackageWriter
from .sandbox.protocol import SandboxBackend
from .specs import HarnessConfig


@dataclass
class CompiledAgent:
    agent: Any
    event_bus: EventBus
    session_registry: SessionRegistry
    run_package_writer: RunPackageWriter
    harness: HarnessConfig
    sandbox: SandboxBackend
    approval_channel: ApprovalChannel
    middleware: list[Any] = field(default_factory=list)
    versions: VersionTriple = field(
        default_factory=lambda: VersionTriple(harness="1", deep_agents="unknown", model="unknown")
    )
    principal_mw: PrincipalPropagationMW = field(default_factory=lambda: PrincipalPropagationMW())
    # BEGIN harness-extensibility story 5
    identity_source: IdentitySource | None = None
    # END harness-extensibility story 5
    # BEGIN harness-extensibility story 6
    secret_resolver: SecretResolver | None = None
    # END harness-extensibility story 6
    # BEGIN harness-extensibility story 7
    model_provider: ModelProvider | None = None
    # END harness-extensibility story 7
    # BEGIN harness-extensibility phase 6
    # Direct handles to the plugin-resolved state surfaces. These are the
    # ABC-based instances — the legacy Protocol-based checkpointer remains
    # reachable via ``session_registry.checkpointer`` for zero-change bots.
    memory_store: MemoryStore | None = None
    job_storage: JobStorage | None = None
    checkpointer_ext: ExtensionsCheckpointer | None = None
    # END harness-extensibility phase 6
    accumulator: RunPackageAccumulator | None = None
    subagent_hooks: SubagentInvocationHooks | None = None

    @property
    def checkpointer(self) -> Any:
        """Back-compat: return the legacy Protocol-based checkpointer in use.

        Tests and external tooling reach the configured checkpointer via this
        property. Resolves through ``session_registry.checkpointer`` so both
        legacy and ABC-based checkpointers are exposed on a single attribute.
        """
        if self.checkpointer_ext is not None:
            return self.checkpointer_ext
        return self.session_registry.checkpointer

    def middleware_names(self) -> list[str]:
        return [getattr(mw, "name", type(mw).__name__) for mw in self.middleware]

    async def ainvoke(
        self,
        messages: Sequence[dict],
        *,
        session_id: str | None = None,
        principal: Principal | None = None,
        # BEGIN harness-extensibility story 5
        identity: LoadedIdentity | None = None,
        # END harness-extensibility story 5
    ) -> dict:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        if sid not in self.session_registry._sessions:  # noqa: SLF001 — intentional internal access
            await self.session_registry.register(
                sid,
                principal=principal or Principal(),
                agent_name=self.harness.agent.name,
                token_budget=self.harness.context.token_budget,
                versions=self.versions,
            )
        state = self.session_registry.ensure_active(sid)
        started = datetime.now(UTC)
        outcome = "pass"
        inputs = [dict(m) for m in messages]
        outputs: list[dict] = []
        hooks_tok: Any = None
        with self.principal_mw.scope(principal=state.principal, run_id=run_id, session_id=sid):
            if self.accumulator is not None:
                self.accumulator.begin_root(run_id, sid, state.principal, self.versions, started, inputs)
                if self.subagent_hooks is not None:
                    hooks_tok = set_active_subagent_hooks(self.subagent_hooks)
            await self.event_bus.publish(
                HarnessEvent(
                    run_id=run_id,
                    session_id=sid,
                    principal=state.principal,
                    versions=self.versions,
                    ts=started,
                    kind=EventKind.AGENT_START,
                    payload={"agent": self.harness.agent.name, "messages": len(inputs)},
                )
            )
            try:
                result = await self._invoke_agent(messages)
                outputs = _as_message_list(result)
            except Exception as exc:  # noqa: BLE001
                outcome = "fail"
                await self.event_bus.publish(
                    HarnessEvent(
                        run_id=run_id,
                        session_id=sid,
                        principal=state.principal,
                        versions=self.versions,
                        ts=datetime.now(UTC),
                        kind=EventKind.ERROR,
                        payload={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
                raise
            finally:
                ended = datetime.now(UTC)
                await self.event_bus.publish(
                    HarnessEvent(
                        run_id=run_id,
                        session_id=sid,
                        principal=state.principal,
                        versions=self.versions,
                        ts=ended,
                        kind=EventKind.AGENT_END,
                        payload={"duration_ms": int((ended - started).total_seconds() * 1000), "outcome": outcome},
                    )
                )
                try:
                    if self.accumulator is not None:
                        await self.accumulator.flush_deferred_events(self.event_bus)
                        pkg = self.accumulator.complete_root(outputs, outcome, ended)
                    else:
                        pkg = RunPackage(
                            run_id=run_id,
                            session_id=sid,
                            principal=state.principal,
                            versions=self.versions,
                            started_at=started,
                            ended_at=ended,
                            inputs=inputs,
                            outputs=outputs,
                            outcome=outcome,  # type: ignore[arg-type]
                        )
                    await self.run_package_writer.write(pkg)
                    await self.event_bus.publish(
                        HarnessEvent(
                            run_id=run_id,
                            session_id=sid,
                            principal=state.principal,
                            versions=self.versions,
                            ts=datetime.now(UTC),
                            kind=EventKind.TASK_COMPLETE if outcome == "pass" else EventKind.TASK_FAILED,
                            payload={"run_id": run_id, "outcome": outcome},
                        )
                    )
                except Exception:  # pragma: no cover
                    pass
                if hooks_tok is not None:
                    reset_active_subagent_hooks(hooks_tok)
            return {"run_id": run_id, "session_id": sid, "messages": outputs, "outcome": outcome}

    async def _invoke_agent(self, messages: Sequence[dict]) -> Any:
        agent = self.agent
        if hasattr(agent, "ainvoke"):
            return await agent.ainvoke({"messages": list(messages)})
        if hasattr(agent, "invoke"):
            return agent.invoke({"messages": list(messages)})
        raise RuntimeError("compiled agent has neither .ainvoke nor .invoke")

    async def astream(
        self,
        messages: Sequence[dict],
        *,
        session_id: str | None = None,
        principal: Principal | None = None,
    ):
        agent = self.agent
        if not hasattr(agent, "astream"):
            result = await self.ainvoke(messages, session_id=session_id, principal=principal)
            yield result
            return
        async for chunk in agent.astream({"messages": list(messages)}):
            yield chunk


def _as_message_list(result: Any) -> list[dict]:
    if isinstance(result, dict) and "messages" in result:
        out: list[dict] = []
        for m in result["messages"]:
            if isinstance(m, dict):
                out.append(m)
            else:
                out.append({"role": getattr(m, "type", "assistant"), "content": getattr(m, "content", str(m))})
        return out
    if isinstance(result, list):
        return [m if isinstance(m, dict) else {"role": "assistant", "content": str(m)} for m in result]
    return [{"role": "assistant", "content": str(result)}]
