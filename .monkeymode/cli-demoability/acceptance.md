# Acceptance

- A fresh `new --provider fake --model fake-model --yes` configures
  `web_search.backend: none`; an existing config is not changed except for the
  current provider/model update behavior.
- `validate` succeeds without warnings for disabled scaffolded MCP examples and
  never prints `mcp.json must have mcpServers object` when that object exists.
- Enabling an MCP server with an unset referenced variable still reports the
  warning.
- `doctor` succeeds for `fake`, reports that credentials are not required, and
  provides no credential remediation.
- Human output and `--json` agree on all statuses and messages.
- Relevant CLI and scaffold tests pass.
