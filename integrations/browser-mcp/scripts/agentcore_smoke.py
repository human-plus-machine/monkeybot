#!/usr/bin/env python3
"""Manual smoke test for the AgentCore browser backend. Not part of pytest.

Requires AWS SSO/credentials already configured (e.g. ``aws sso login``) and
the ``agentcore`` extra installed (``uv sync --extra agentcore --dev``).

Usage:
    AWS_PROFILE=... AWS_REGION=us-east-1 uv run python scripts/agentcore_smoke.py
"""

from __future__ import annotations

from browser_mcp import agentcore, playwright_helpers


def main() -> int:
    region = agentcore.resolve_region()
    admin = agentcore.AgentCoreAdmin(region)

    print(f"[1/5] ensure_session (region={region}, identifier={admin.identifier}) ...")
    ws_url, headers = admin.ensure_session()
    print(f"      got ws_url={ws_url!r} with {len(headers)} signed header(s)")

    try:
        print("[2/5] connecting Playwright over CDP ...")
        playwright_helpers.connect(ws_url, headers)

        print("[3/5] navigating to https://example.com ...")
        playwright_helpers.new_tab("https://example.com")
        playwright_helpers.wait_for_load()
        info = playwright_helpers.page_info()
        print(f"      page_info: {info}")

        print("[4/5] capturing screenshot ...")
        path = playwright_helpers.capture_screenshot(path="agentcore_smoke.png", max_dim=1800)
        print(f"      saved {path}")
    finally:
        print("[5/5] stopping session ...")
        playwright_helpers.disconnect()
        admin.stop_session()

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
