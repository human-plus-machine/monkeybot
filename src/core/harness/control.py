"""SessionRegistry + IntrospectionReport — the control plane's in-memory state store.

Persists session lifecycle (active / paused / cancelled / revoked) and exposes
operator actions (pause / resume / cancel / revoke / rewind).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .checkpointer import CheckpointerBackend, CheckpointRef
from .event_bus import EventBus
from .events import EventKind, HarnessEvent, Principal, VersionTriple
from .errors import HarnessError

SessionStatus = Literal["active", "paused", "cancelled", "revoked"]


class IntrospectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    principal: Principal
    agent_name: str
    status: SessionStatus
    identity_files_loaded: list[str] = Field(default_factory=list)
    active_skills: list[str] = Field(default_factory=list)
    active_mcp_servers: list[str] = Field(default_factory=list)
    permissions: dict[str, Any] = Field(default_factory=dict)
    token_budget: int = 0
    utilization_pct: float = 0.0
    versions: VersionTriple | None = None
    checkpoints: list[CheckpointRef] = Field(default_factory=list)


@dataclass
class SessionState:
    session_id: str
    principal: Principal
    agent_name: str
    status: SessionStatus = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revoked_reason: str | None = None
    identity_files_loaded: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    active_mcp_servers: list[str] = field(default_factory=list)
    permissions: dict[str, Any] = field(default_factory=dict)
    token_budget: int = 0
    utilization_pct: float = 0.0
    versions: VersionTriple | None = None


class SessionRegistry:
    def __init__(self, checkpointer: CheckpointerBackend, event_bus: EventBus) -> None:
        self.checkpointer = checkpointer
        self.event_bus = event_bus
        self._sessions: dict[str, SessionState] = {}

    async def register(
        self,
        session_id: str,
        *,
        principal: Principal,
        agent_name: str,
        token_budget: int = 0,
        versions: VersionTriple | None = None,
    ) -> SessionState:
        state = SessionState(
            session_id=session_id,
            principal=principal,
            agent_name=agent_name,
            token_budget=token_budget,
            versions=versions,
        )
        self._sessions[session_id] = state
        return state

    def ensure_active(self, session_id: str) -> SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            raise HarnessError(f"unknown session {session_id}")
        if state.status == "revoked":
            raise HarnessError(f"session {session_id} is revoked: {state.revoked_reason}")
        if state.status == "cancelled":
            raise HarnessError(f"session {session_id} is cancelled")
        return state

    async def pause(self, session_id: str) -> SessionState:
        state = self._sessions[session_id]
        state.status = "paused"
        return state

    async def resume(self, session_id: str) -> SessionState:
        state = self._sessions[session_id]
        if state.status == "revoked":
            raise HarnessError(f"cannot resume revoked session {session_id}")
        state.status = "active"
        return state

    async def cancel(self, session_id: str) -> SessionState:
        state = self._sessions[session_id]
        state.status = "cancelled"
        return state

    async def revoke(self, session_id: str, reason: str) -> SessionState:
        state = self._sessions[session_id]
        state.status = "revoked"
        state.revoked_reason = reason
        return state

    async def rewind(self, session_id: str, checkpoint_id: str) -> Any:
        restored = await self.checkpointer.read(session_id, checkpoint_id)
        if restored is None:
            raise HarnessError(f"checkpoint {checkpoint_id} not found for session {session_id}")
        await self.event_bus.publish(
            HarnessEvent(
                run_id="rewind",
                session_id=session_id,
                principal=self._sessions[session_id].principal,
                versions=self._sessions[session_id].versions or VersionTriple(harness="1", deep_agents="n/a", model="n/a"),
                ts=datetime.now(UTC),
                kind=EventKind.CHECKPOINT_RESTORE,
                payload={"checkpoint_id": checkpoint_id},
            )
        )
        return restored

    async def checkpoint(self, session_id: str, state: Any, *, reason: str) -> CheckpointRef:
        state_ref = self._sessions[session_id]
        ref = await self.checkpointer.write(session_id, state, reason=reason)
        await self.event_bus.publish(
            HarnessEvent(
                run_id=reason,
                session_id=session_id,
                principal=state_ref.principal,
                versions=state_ref.versions or VersionTriple(harness="1", deep_agents="n/a", model="n/a"),
                ts=datetime.now(UTC),
                kind=EventKind.CHECKPOINT_WRITE,
                payload={"checkpoint_id": ref.id, "reason": reason},
            )
        )
        return ref

    async def introspect(self, session_id: str) -> IntrospectionReport:
        state = self._sessions[session_id]
        checkpoints = await self.checkpointer.list(session_id)
        return IntrospectionReport(
            session_id=state.session_id,
            principal=state.principal,
            agent_name=state.agent_name,
            status=state.status,
            identity_files_loaded=state.identity_files_loaded,
            active_skills=state.active_skills,
            active_mcp_servers=state.active_mcp_servers,
            permissions=state.permissions,
            token_budget=state.token_budget,
            utilization_pct=state.utilization_pct,
            versions=state.versions,
            checkpoints=checkpoints,
        )

    async def list_sessions(
        self,
        *,
        principal: str | None = None,
        status: SessionStatus | None = None,
    ) -> list[SessionState]:
        out = list(self._sessions.values())
        if principal is not None:
            out = [s for s in out if s.principal.id == principal]
        if status is not None:
            out = [s for s in out if s.status == status]
        return out
