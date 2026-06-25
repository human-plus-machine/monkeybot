# ruff: noqa: INP001 — example entrypoint, not a package
"""AWS Bedrock AgentCore (Pattern C) — thin adapter around :func:`~monkeybot.core.bootstrap.run_pattern_bc_turn`.

AgentCore invokes your handler with a JSON ``event``; shapes vary by agent configuration.
This example reads ``sessionId`` / ``session_id`` and ``inputText`` / ``message`` / ``prompt``,
runs one harness turn, and returns a small JSON envelope you can map into AgentCore's
required response format for your action group.

Environment: same as ``examples/lambda/handler.py``.

``hook_manager=None``: see ``examples/lambda/handler.py`` module docstring.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from monkeybot.core.bootstrap import (
    HarnessDeps,
    PatternBcTurnError,
    create_harness_deps,
    run_pattern_bc_turn,
)
from monkeybot.core.workspace_layout import resolve_agent_workspace_root

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


def _extract_message(event: dict[str, Any]) -> str:
    for key in ("inputText", "message", "prompt", "body"):
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


async def _run_turn(event: dict[str, Any]) -> dict[str, Any]:
    deps = await _ensure_deps()
    message = _extract_message(event)
    if not message:
        return {"ok": False, "error": "missing message"}

    session_id = str(
        event.get("sessionId") or event.get("session_id") or event.get("sessionID") or "default"
    )
    request_id = str(event.get("request_id") or uuid.uuid4())

    agent_md = Path(os.environ["AGENT_MD_PATH"])
    skills = Path(os.environ["SKILLS_PATH"])
    workspace_root = resolve_agent_workspace_root()

    try:
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
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup"),
                "apiPath": event.get("apiPath"),
                "httpStatusCode": 500,
                "responseBody": {
                    "TEXT": {
                        "body": json.dumps(
                            {"ok": False, "error": str(exc), "request_id": exc.request_id}
                        )
                    }
                },
            },
        }
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "apiPath": event.get("apiPath"),
            "httpStatusCode": 200,
            "responseBody": {"TEXT": {"body": json.dumps({"ok": True, "text": text})}},
        },
    }


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Sync entry; AgentCore runtimes without native async can use this."""
    del context
    return asyncio.run(_run_turn(event))
