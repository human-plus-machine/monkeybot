"""AWS Bedrock AgentCore Browser backend admin: session lifecycle only.

Kept Playwright-agnostic on purpose -- this module owns starting/stopping the
AgentCore browser session (StartBrowserSession / StopBrowserSession via the
``bedrock_agentcore`` SDK) and handing back signed CDP WebSocket credentials.
Connecting Playwright to those credentials lives in ``playwright_helpers``.

Spike finding: browser-harness 0.1.x cannot send the SigV4-signed CDP headers
AgentCore's Automation endpoint requires, so this backend bypasses
browser-harness entirely and drives the browser directly via
``bedrock_agentcore.tools.browser_client.browser_session`` +
Playwright's ``connect_over_cdp``.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_IDENTIFIER = "aws.browser.v1"


def resolve_region() -> str:
    """AWS_REGION, falling back to AWS_DEFAULT_REGION, then us-east-1."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


class AgentCoreAdmin:
    """Owns one AgentCore browser session's lifecycle."""

    def __init__(self, region: str, identifier: str | None = None) -> None:
        self.region = region
        self.identifier = identifier or os.environ.get("AGENTCORE_BROWSER_ID", DEFAULT_IDENTIFIER)
        self._session_cm: Any = None
        self._client: Any = None

    def ensure_session(self) -> tuple[str, dict[str, str]]:
        """Idempotent: start the AgentCore session if not already up.

        Returns ``(ws_url, headers)`` from the session client's
        ``generate_ws_headers()`` -- ready for Playwright's
        ``connect_over_cdp(ws_url, headers=headers)``. Session TTL is left at
        whatever the bedrock-agentcore SDK/service default is -- not
        configured here.
        """
        if self._client is None:
            from bedrock_agentcore.tools.browser_client import browser_session

            self._session_cm = browser_session(self.region, identifier=self.identifier)
            self._client = self._session_cm.__enter__()
        return self._client.generate_ws_headers()

    def stop_session(self) -> None:
        """Idempotent: StopBrowserSession via the session context manager's __exit__."""
        if self._session_cm is None:
            return
        cm, self._session_cm, self._client = self._session_cm, None, None
        cm.__exit__(None, None, None)


def agentcore_backend_requested() -> bool:
    """True iff BROWSER_BACKEND=agentcore and no explicit CDP endpoint is set.

    An operator-supplied BU_CDP_WS/BU_CDP_URL (including one set by
    server._apply_in_app_cdp_url() from Monkeyapp's in-app bridge) always
    wins over the agentcore backend -- explicit CDP configuration is more
    specific than the opt-in backend flag.
    """
    if (os.environ.get("BROWSER_BACKEND") or "").strip().lower() != "agentcore":
        return False
    ws = (os.environ.get("BU_CDP_WS") or "").strip()
    http = (os.environ.get("BU_CDP_URL") or "").strip()
    return not (ws or http)
