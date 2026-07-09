---
name: browser
description: Control a real browser via CDP for web tasks; check and write site playbooks before improvising.
---

# browser

See the demo agent copy at `demo_agent/workspace/skills/browser/SKILL.md` for the full agent workflow and **production deployment** options (local dev, self-hosted headless Chromium, Browser Use Cloud).

Copy this folder into your configured `SKILLS_PATH` (needs `SKILL.md` and a `playbooks/` subdirectory).

Enable the `browser` MCP server in `monkeybot_config/mcp.json` and install the separate `integrations/browser-mcp` package (`uv sync --project integrations/browser-mcp`).

Operator reference: `docs/browser-mcp.md` in the monkeybot repo.
