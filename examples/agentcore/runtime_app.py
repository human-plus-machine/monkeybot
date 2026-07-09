# ruff: noqa: INP001 — example entrypoint, not a package
"""AWS Bedrock AgentCore Runtime HTTP adapter (container on port 8080).

Run locally:

    uvicorn examples.agentcore.runtime_app:app --host 0.0.0.0 --port 8080

AgentCore expects ``GET /ping`` and ``POST /invocations``. See ``docs/deploy-aws-agentcore.md``.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from monkeybot.core.bootstrap import (
    HarnessDeps,
    PatternBcTurnError,
    create_harness_deps,
    run_pattern_bc_turn,
)
from monkeybot.core.workspace_layout import resolve_agent_workspace_root

app = FastAPI(title="monkeybot AgentCore Runtime")
_deps: HarnessDeps | None = None


async def _ensure_deps() -> HarnessDeps:
    global _deps
    if _deps is None:
        mcp_path = os.environ.get("MCP_CONFIG")
        open_mcp = os.environ.get("MONKEYBOT_OPEN_MCP", "").lower() in ("1", "true", "yes")
        _deps = await create_harness_deps(
            os.environ["DB_URL"],
            os.environ.get("MEMORY_STORAGE_URI"),
            mcp_config_path=Path(mcp_path) if mcp_path else None,
            open_mcp=open_mcp,
        )
    return _deps


def _extract_message(body: dict[str, Any]) -> str:
    for key in ("inputText", "message", "prompt", "body"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


@app.get("/ping")
async def ping() -> dict[str, Any]:
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


@app.post("/invocations")
async def invocations(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "body must be a JSON object"})

    message = _extract_message(body)
    if not message:
        return JSONResponse(status_code=400, content={"ok": False, "error": "missing message"})

    session_id = str(
        body.get("sessionId") or body.get("session_id") or body.get("sessionID") or "default"
    )
    request_id = str(body.get("request_id") or uuid.uuid4())

    agent_md = Path(os.environ["AGENT_MD_PATH"])
    skills = Path(os.environ["SKILLS_PATH"])
    workspace_root = resolve_agent_workspace_root()

    try:
        deps = await _ensure_deps()
        text = await run_pattern_bc_turn(
            deps,
            message,
            session_id=session_id,
            request_id=request_id,
            agent_md_path=agent_md,
            skills_path=skills,
            workspace_root=workspace_root,
            hook_manager=None,
        )
    except PatternBcTurnError as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "request_id": exc.request_id},
        )
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "text": text, "response": text})
