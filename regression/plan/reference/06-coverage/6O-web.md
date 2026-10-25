# 6.O Web search and browser MCP (`WEB`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `web_search/*`. Browser MCP is a separate package (`integrations/browser-mcp`) with its own suite; it stays out of scope here except the harness hook.

| ID | Case | Asserts |
|---|---|---|
| T2-WEB-01 | `web_search` (migrate, fake backend, real tool) | Result JSON `ok:true`, `query` echoed, results passed through. |
| T2-WEB-02 | `empty_query` | `ok:false`; backend not called. |
| T2-WEB-03 | `backend_none_not_advertised` | `WEB_SEARCH_BACKEND=none`: tool absent. |
| T2-WEB-04 | `max_results` | `WEB_SEARCH_MAX_RESULTS=2`: two results. |
| T2-WEB-05 | `browser_stop_on_disconnect` | Disconnecting a server named `browser` calls `browser_stop` first (harness side only). |
