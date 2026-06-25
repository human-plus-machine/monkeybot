# ruff: noqa: INP001 — example deployable class, not a package
"""Vertex AI Agent Engine (Pattern C) — class with ``query`` for Reasoning Engine deployment.

When you package this class for Agent Engine, Vertex calls ``query`` on each user request.
Keyword arguments are forwarded from the platform (typical keys: ``message``, ``prompt``,
``session_id``).

Environment: same as ``examples/lambda/handler.py``.

``hook_manager=None``: see ``examples/lambda/handler.py`` module docstring.

``query`` is synchronous because many Reasoning Engine templates expect a blocking entry; it
uses ``asyncio.run`` internally. If your deployment supports async handlers, you can replace
this with a native ``async def query``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

from monkeybot.core.bootstrap import HarnessDeps, create_harness_deps, run_pattern_bc_turn
from monkeybot.core.workspace_layout import resolve_agent_workspace_root


class MonkeybotReasoningEngine:
    """Minimal Agent Engine adapter — copy into your deployment package."""

    def __init__(self) -> None:
        self._deps: HarnessDeps | None = None

    async def _ensure_deps(self) -> HarnessDeps:
        if self._deps is None:
            mcp_path = os.environ.get("MCP_CONFIG")
            open_mcp = os.environ.get("MONKEYBOT_OPEN_MCP", "").lower() in ("1", "true", "yes")
            self._deps = await create_harness_deps(
                os.environ["DB_URL"],
                os.environ.get("MEMORY_STORAGE_URI"),
                mcp_config_path=Path(mcp_path) if mcp_path else None,
                open_mcp=open_mcp,
            )
        return self._deps

    async def _run_turn_async(self, **kwargs: Any) -> dict[str, Any]:
        deps = await self._ensure_deps()

        message = str(
            kwargs.get("message") or kwargs.get("prompt") or kwargs.get("input") or ""
        ).strip()
        if not message:
            return {"ok": False, "error": "missing message"}

        session_id = str(kwargs.get("session_id") or kwargs.get("sessionId") or uuid.uuid4())
        request_id = str(kwargs.get("request_id") or uuid.uuid4())

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

    def query(self, **kwargs: Any) -> dict[str, Any]:
        """Vertex Reasoning Engine entry — keyword args come from ``engine.query(...)``."""
        return asyncio.run(self._run_turn_async(**kwargs))
