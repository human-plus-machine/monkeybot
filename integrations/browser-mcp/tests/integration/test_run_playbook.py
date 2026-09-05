"""Live-browser checks for executable playbooks.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BROWSER_MCP_INTEGRATION") != "1",
    reason="set BROWSER_MCP_INTEGRATION=1 to run live browser tests",
)


def test_run_playbook_signup_on_form(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import playbooks, server

    host = "127.0.0.1"
    md = f"""```playbook
name: signup
params: [nickname]
steps:
  - {{do: goto, url: "{fixture_server}/form.html"}}
  - {{do: fill_form, fields: {{Nickname: "{{{{nickname}}}}"}}, submit: true}}
expect:
  text: "Thanks, form submitted."
```
"""
    playbooks.write_playbook(host, md)
    result = json.loads(
        server.browser_run_playbook(host, "signup", {"nickname": "playnick"}, observe="full")
    )
    assert result["ok"] is True
    assert result["name"] == "signup"
    assert result.get("observation")
    status = json.loads(server.browser_js("!document.getElementById('status').hidden"))
    assert status.get("result") is True


def test_run_playbook_stale_click_text(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import playbooks, server

    host = "127.0.0.1"
    md = f"""```playbook
name: stale
steps:
  - {{do: goto, url: "{fixture_server}/form.html"}}
  - {{do: click_text, text: "No Such Button"}}
```
"""
    playbooks.write_playbook(host, md)
    result = json.loads(server.browser_run_playbook(host, "stale", observe="full"))
    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert result.get("observation")
