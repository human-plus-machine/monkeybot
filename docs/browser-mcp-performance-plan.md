# Browser MCP — leftover performance work

**Status:** Phases 0–6, 7a, and 8 shipped on `feat/browser-optimization`. This file is only the work that did not land.

**Related:** [browser-mcp.md](browser-mcp.md) · [browser-mcp-perf-baseline.md](browser-mcp-perf-baseline.md)

---

### Phase 7b — inline MCP images (deferred)

Allow MCP tools to return images inline so a screenshot-to-click path does not need a separate `load_file` turn. That is a **core** change (`core/mcp/mcp_client.py` result flattening, `core_tool_executor.py` `_media_result`, `_IMAGE_CAPABLE_TOOLS`), not browser-mcp. Ship as a separate PR.

### Phase 9 — harness IPC (upstream, optional)

Remove the per-request socket connect and 64 KiB line limit in `browser-use/browser-harness`. Prefer an upstream PR, then bump the pin. Feature-detect `helpers.batch` in browser-mcp; Phases 1–8 already minimized the call count.

Acceptance: `perf_bench.py` wall time per tool down a further ≥ 30 % on the local daemon.
