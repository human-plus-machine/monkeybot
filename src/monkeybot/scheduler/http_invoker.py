"""HTTP tick invoker for standalone scheduler workers."""

from __future__ import annotations

import logging

import httpx

from monkeybot.core.types.content_blocks import ContentBlock, Text

logger = logging.getLogger(__name__)


class HttpTickInvoker:
    """Call the gateway ``/scheduler/invoke-tick`` endpoint synchronously."""

    def __init__(self, base_url: str, *, timeout_s: float = 3600.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> str | None:
        message = "\n".join(
            block.text for block in user_content if isinstance(block, Text)
        )
        payload = {
            "session_id": session_id,
            "request_id": request_id,
            "message": message,
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            try:
                session_resp = await client.post(
                    f"{self._base_url}/sessions",
                    json={"session_id": session_id},
                )
                if session_resp.status_code not in (201, 409):
                    return f"create session failed: {session_resp.status_code} {session_resp.text}"
                resp = await client.post(f"{self._base_url}/scheduler/invoke-tick", json=payload)
            except httpx.HTTPError as exc:
                return str(exc)
        if resp.status_code == 409:
            return "session busy"
        if resp.status_code >= 400:
            return f"invoke-tick failed: {resp.status_code} {resp.text}"
        return None
