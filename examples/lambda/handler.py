# ruff: noqa: INP001 — example entrypoint, not a package
"""AWS Lambda (Pattern B) — harness-as-library without FastAPI.

Environment (minimum):
  DB_URL              — sqlite or postgres URL (see deploy-pattern-b-serverless.md)
  AGENT_MD_PATH       — path to AGENT.md
  SKILLS_PATH         — skills root directory
  WORKSPACE_ROOT      — repo/workspace root for tools

Optional:
  MEMORY_STORAGE_URI  — local://, gcs://, s3:// (see Step 2 docs)
  MCP_CONFIG          — path to mcpServers JSON; if unset, MCP stays empty
  MONKEYBOT_OPEN_MCP  — set to "true" to load MCP_CONFIG (default: false for Lambda)

Model/provider: use MODEL_PROVIDER, MODEL_NAME, and provider secrets as in container deploys.

``hook_manager=None`` (via :func:`~monkeybot.core.bootstrap.run_pattern_bc_turn`) means the
agent loop does not run :class:`~monkeybot.core.hooks.HookManager` callbacks, so automatic
memory hook capture (recall / writer drain) is off; MemPalace wake-up
still works. Enable hooks in FaaS only if you use positive timeouts so work completes before return.

This example uses ``async def lambda_handler`` (Python 3.9+ on Lambda). If you must use a
sync ``def handler``, bootstrap with ``asyncio.new_event_loop()`` and ``run_until_complete``
for cold start and per-invocation coroutines instead of ``asyncio.run()`` (destroys the loop).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from monkeybot.core.bootstrap import HarnessDeps, create_harness_deps, run_pattern_bc_turn
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


async def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    del context
    deps = await _ensure_deps()

    message = str(event.get("message") or event.get("body") or "").strip()
    if not message:
        return {"ok": False, "error": "missing message"}

    session_id = str(event.get("session_id") or "default")
    request_id = str(event.get("request_id") or uuid.uuid4())

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
