# 6.N MCP (`MCP`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`.

Existing: T1 `test_mcp_client.py`, `test_mcp_oauth*.py`, executor MCP tests, `test_context.py`. No T2. T4 `mcp/tool_invoke`, `list_resources`, `get_prompt` against `fixture_mcp_server.py` with `autoConnect: true`, so progressive disclosure is never exercised live.

| ID | Case | Asserts |
|---|---|---|
| T2-MCP-01 | `enable_then_call` | Progressive config: request 1 lacks `fixture__echo`; `enable_mcp fixture`; request 2 includes it; call returns echo; `provider_calls: 3`. |
| T2-MCP-02 | `disable_removes_tools` | `disable_mcp`: tool gone from the next request. |
| T2-MCP-03 | `meta_tools_only_when_connected` | Resource/prompt tools absent before connect, present after. |
| T2-MCP-04 | `resources_and_prompts` | `list_mcp_resources` / `read_mcp_resource fixture://readme` / `list_mcp_prompts` / `get_mcp_prompt greet`. |
| T2-MCP-05 | `autoconnect_failure_is_status` | Broken sibling server: load returns; status failed; good server still connected. |
| T2-MCP-06 | `strict_load_rolls_back` | `MCP_STRICT_LOAD`: failure aborts and siblings roll back. |
| T2-MCP-07 | `name_prefix_split` | Servers `a` and `a_b`: `a_b__tool` routes to `a_b`. |
| T2-MCP-08 | `env_interpolation_missing` | `${MISSING}` becomes `""`, server still loads. |
| T2-MCP-09 | `catalog_diff_reload` | Edit `mcp.json`: changed server reconnects; bad JSON keeps existing connections. |
| T2-MCP-10 | `enable_unknown_server` | Not in catalog: error string (pinned; not an envelope). |
| T2-MCP-11 | `disabled_server_not_catalogued` | `enabled: false`: not offered. |
| T3-MCP-01 | `replay_tools_list_call` | Recorded `tools/list` + `tools/call` JSON: same prefixed names and normalized result text. |
| T4-MCP-01..03 | `mcp/*` (migrate) | autoConnect path as today. |
| T4-MCP-04 | `enable_then_echo_live` | Progressive config: `required_tools: [enable_mcp, fixture__echo]`, `max_tool_errors: 0`. |
| T5-MCP-01 | trials of T4-MCP-04 | Capability. |
