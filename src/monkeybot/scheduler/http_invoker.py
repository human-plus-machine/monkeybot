"""HTTP tick invoker for standalone scheduler workers."""

from __future__ import annotations

import httpx

from monkeybot.core.types.content_blocks import ContentBlock, Text
from monkeybot.scheduler.tick_result import TickInvokeResult


class HttpTickInvoker:
    """Call the gateway ``/scheduler/invoke-tick`` endpoint synchronously."""

    def __init__(self, base_url: str, *, timeout_s: float = 3600.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._sessions_ready: set[str] = set()

    async def _ensure_session(self, client: httpx.AsyncClient, session_id: str) -> TickInvokeResult | None:
        if session_id in self._sessions_ready:
            return None
        session_resp = await client.post(
            f"{self._base_url}/sessions",
            json={"session_id": session_id},
        )
        if session_resp.status_code in (201, 409):
            self._sessions_ready.add(session_id)
            return None
        return TickInvokeResult.fail(
            f"create session failed: {session_resp.status_code} {session_resp.text}"
        )

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
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
                ensure_err = await self._ensure_session(client, session_id)
                if ensure_err is not None:
                    return ensure_err
                resp = await client.post(f"{self._base_url}/scheduler/invoke-tick", json=payload)
            except httpx.HTTPError as exc:
                return TickInvokeResult.fail(str(exc))
        if resp.status_code == 409:
            return TickInvokeResult.session_busy()
        if resp.status_code >= 400:
            return TickInvokeResult.fail(
                f"invoke-tick failed: {resp.status_code} {resp.text}"
            )
        return TickInvokeResult.ok()
