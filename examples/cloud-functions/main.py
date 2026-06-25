# ruff: noqa: INP001 — example entrypoint, not a package
"""GCP Cloud Functions (2nd gen, Python 3.12) — Pattern B harness-as-library.

Environment: same as ``examples/lambda/handler.py`` (DB_URL, AGENT_MD_PATH, SKILLS_PATH,
WORKSPACE_ROOT, optional MEMORY_STORAGE_URI / MCP_CONFIG / MONKEYBOT_OPEN_MCP).

``hook_manager=None``: see module docstring on ``examples/lambda/handler.py`` (hook-driven
memory capture is off unless you wire a ``HookManager``).

Entry: ``handler`` is synchronous; each invocation uses ``asyncio.run`` for the async harness.
Cold-start deps are cached in a module global across warm invocations.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from monkeybot.core.bootstrap import HarnessDeps, create_harness_deps, run_pattern_bc_turn
from monkeybot.core.workspace_layout import resolve_agent_workspace_root

_deps: HarnessDeps | None = None


async def _open_deps() -> HarnessDeps:
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


async def _handle_async(request_json: dict[str, Any]) -> dict[str, Any]:
    deps = await _open_deps()

    message = str(request_json.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "missing message"}

    session_id = str(request_json.get("session_id") or "default")
    request_id = str(request_json.get("request_id") or uuid.uuid4())

    agent_md = Path(os.environ["AGENT_MD_PATH"])
    skills = Path(os.environ["SKILLS_PATH"])
    workspace_root = resolve_agent_workspace_root()

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
    return {"ok": True, "session_id": session_id, "text": text}


def handler(request: Any) -> dict[str, Any]:
    """HTTP Cloud Function entrypoint."""
    body = (
        request.get_json(silent=True) or {}
        if hasattr(request, "get_json")
        else {}
    )
    return asyncio.run(_handle_async(body))
