"""AgentCore invocation adapter.

Implements the Bedrock AgentCore Runtime contract on top of the harness:
    POST /agentcore/invocations     (streaming or non-streaming)
    GET  /agentcore/ping
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.core.harness.principal import ANONYMOUS, Principal

router = APIRouter(prefix="/agentcore", tags=["agentcore"])


class AgentCoreSessionState(BaseModel):
    sessionAttributes: dict[str, Any] = Field(default_factory=dict)
    promptSessionAttributes: dict[str, Any] = Field(default_factory=dict)


class AgentCoreInvocationsRequest(BaseModel):
    inputText: str
    sessionId: str | None = None
    enableTrace: bool = False
    endSession: bool = False
    sessionState: AgentCoreSessionState | None = None


class AgentCoreTrace(BaseModel):
    runId: str
    events: list[dict[str, Any]] = Field(default_factory=list)


class AgentCoreInvocationsResponse(BaseModel):
    completion: str
    sessionId: str
    trace: AgentCoreTrace | None = None


@router.get("/ping")
async def ping() -> dict:
    return {"status": "ok"}


@router.post("/invocations")
async def invocations(body: AgentCoreInvocationsRequest, req: Request) -> Any:
    compiled = getattr(req.app.state, "compiled_agent", None)
    if compiled is None:
        raise HTTPException(503, "compiled_agent not mounted on app.state")

    session_id = body.sessionId or "agentcore-anon"
    principal = _principal_from_session_state(body.sessionState)

    if _wants_streaming(req):
        return StreamingResponse(
            _stream(compiled, body, session_id, principal),
            media_type="text/event-stream",
        )

    result = await compiled.ainvoke(
        [{"role": "user", "content": body.inputText}],
        session_id=session_id,
        principal=principal,
    )
    completion = _extract_completion(result["messages"])
    trace = AgentCoreTrace(runId=result["run_id"], events=[]) if body.enableTrace else None
    return AgentCoreInvocationsResponse(
        completion=completion,
        sessionId=session_id,
        trace=trace,
    )


def _principal_from_session_state(state: AgentCoreSessionState | None) -> Principal:
    if state is None:
        return ANONYMOUS
    user_id = state.sessionAttributes.get("user_id") if state.sessionAttributes else None
    if user_id:
        return Principal(kind="user", id=str(user_id))
    return ANONYMOUS


def _wants_streaming(req: Request) -> bool:
    accept = req.headers.get("accept", "")
    return "text/event-stream" in accept


def _extract_completion(messages: list[dict]) -> str:
    for m in reversed(messages):
        role = m.get("role") or m.get("type")
        if role in ("assistant", "ai"):
            content = m.get("content")
            if isinstance(content, str):
                return content
            return str(content)
    return ""


async def _stream(
    compiled: Any,
    body: AgentCoreInvocationsRequest,
    session_id: str,
    principal: Principal,
) -> AsyncIterator[bytes]:
    async def _run() -> dict:
        return await compiled.ainvoke(
            [{"role": "user", "content": body.inputText}],
            session_id=session_id,
            principal=principal,
        )

    task = asyncio.create_task(_run())
    while not task.done():
        await asyncio.sleep(0.05)
        yield b"event: chunk\ndata: {\"status\":\"in_progress\"}\n\n"
    result = await task
    completion = _extract_completion(result["messages"])
    yield (
        b"event: end\ndata: "
        + json.dumps(
            {"completion": completion, "sessionId": session_id, "runId": result["run_id"]}
        ).encode("utf-8")
        + b"\n\n"
    )
