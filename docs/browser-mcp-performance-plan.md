# Browser MCP — Performance Plan

**Status:** Phase 0–4, 7a, and 8 complete — 7b and later phases not implemented  
**Audience:** implementer (human or model) working in `integrations/browser-mcp/`  
**Related:** [browser-mcp.md](browser-mcp.md) · [browser-mcp-perf-baseline.md](browser-mcp-perf-baseline.md) · `integrations/browser-mcp/src/browser_mcp/` · `examples/skills/browser/SKILL.md` · upstream [browser-use/browser-harness](https://github.com/browser-use/browser-harness) (pinned `browser-harness==0.1.5` in `.venv`)  
**Baseline:** Phase 0 numbers in [browser-mcp-perf-baseline.md](browser-mcp-perf-baseline.md), `monkeybot-browser-mcp` 0.5.0  
**Branch:** `feat/browser-optimization` (all phases land here)  
**Version:** one `monkeybot-browser-mcp` minor for the whole plan (`0.4.0` → `0.5.0`, already done in Phase 0). Do **not** bump again in Phases 1–9. Note contract additions in `CHANGELOG.md` under that single release.  
**Goal:** make the harness finish web tasks in fewer model turns, fewer tokens per turn, and less wall time per tool call, without breaking the existing tool contract.

---

## How to use this document

Phases are ordered by value per unit of risk. Each phase is independently shippable as a commit on `feat/browser-optimization`. Within a phase, sections are: **Goal → Why → Changes (file by file) → Contracts → Tests → Acceptance → Risks**. Every claim about current code cites a file and line as of the baseline commit; re-verify line numbers before editing.

Do not skip Phase 0. Every later phase reports its acceptance numbers against the Phase 0 baseline. Do not bump `monkeybot-browser-mcp` again after Phase 0 — `0.5.0` covers Phases 0–9.

---

## Part 1 — Where the time goes today

Three layers, in decreasing cost:

| Layer | Evidence | Cost |
|---|---|---|
| **Model round trips** | The documented loop is `get_elements → click → get_elements → input → get_elements` (`server.py:44-70`, `SKILL.md`). Every step is a full LLM turn. | Seconds per step. Dominates everything. |
| **Tokens per observation** | `browser_get_elements` defaults to `viewport_only=False` (`server.py:404`) and returns the whole tree, untruncated, every call. Core spills oversized tool results to `.monkeybot/spill` (`core_tool_executor.py:117`), costing yet another turn to read back. | Thousands of tokens per call on real pages. |
| **IPC / CDP chatter** | `helpers._send` opens a fresh socket per request (`browser_harness/helpers.py:43-50`); the daemon serves one line per connection and closes (`daemon.py:389-402`). `fill_input` types one char at a time, 2–3 CDP calls each (`helpers.py:177-222`). The 60 KB DOM driver is re-injected in base64 chunks after every navigation (`dom_indexing.py:52-70`). Waits poll every 300 ms with a JS eval each (`helpers.py:362-398`). | Tens to hundreds of ms per tool; ~90 socket connects to type a 30-char string. |

### Hard constraints the implementer must respect

1. **browser-harness is a third-party pinned dependency**, not in this repo. Phases 1–8 use only its public helper surface (`helpers.js`, `helpers.cdp`, `helpers.drain_events`, `helpers.click_at_xy`, `helpers.fill_input`, …). Daemon/IPC changes are isolated in Phase 9 and are optional.
2. **One IPC request per socket, 64 KiB per line.** The daemon's `asyncio.start_unix_server` uses the default `StreamReader` limit (64 KiB). Any single `helpers.js(...)` or `helpers.cdp(...)` payload must stay well under that after JSON escaping. Existing chunking is at `dom_indexing.py:47` (`_INJECT_CHUNK_CHARS = 24_000`).
3. **One active CDP session in the daemon, but per-request session routing is available.** The daemon tracks a single focused tab; any non-`Target.*` request may carry its own `session_id` (`daemon.py:377-379`), which Phase 2 uses to read other tabs without switching. **Two backends share one helper surface.** `server.py` binds either `browser_harness.helpers` (CDP daemon) or `browser_mcp.playwright_helpers` (AgentCore, Playwright). The Playwright backend does **not** expose `cdp` or `drain_events` (`playwright_helpers.py:1-18`). Any new helper call must be feature-detected with `hasattr(helpers, ...)` and given a Playwright equivalent, or degrade gracefully.
4. **Core flattens MCP tool results to text.** `core/mcp/mcp_client.py:575-584` concatenates `text` blocks; only `load_file` produces `Image` blocks (`persistence/thread_summary.py:50`, `core_tool_executor.py:1194`). Returning images inline from this MCP server needs a core change (Phase 7b).
5. **Tool contract is public.** Existing tool names, parameters, and `{"ok": ..., ...}` JSON shapes must keep working. Add parameters with defaults; add new tools; never remove or rename.
6. **Three copies of the browser skill** must be kept in step: `examples/skills/browser/SKILL.md`, `cli/src/monkeybot_cli/scaffold_defaults/browser/SKILL.md`, `evals/smoke_agent/skills/browser/SKILL.md`. They are intentionally different in wording; update the workflow sections in each, not by copying files over each other.
7. **Tests use `MagicMock` helpers** patched via `patch.object(server, "_browser_harness", ...)` (see `tests/test_indexed_tools.py:20-30`). Keep that pattern for unit tests. Anything that runs the JS driver needs a real browser: put it under `tests/integration/`, skipped unless `BROWSER_MCP_INTEGRATION=1`, using the optional `playwright` dependency and static fixtures in `tests/fixtures/*.html`.

---

## Part 2 — Phases

### Phase 0 — Instrumentation and baseline — **completed**

**Status.** Done. Baseline table: [browser-mcp-perf-baseline.md](browser-mcp-perf-baseline.md). `perf.py`, `scripts/perf_bench.py`, and `tests/fixtures/` are in tree.

**Goal.** Measure before optimizing. Every later phase must quote numbers from this tooling.

**Why.** Nothing today records per-tool wall time, harness calls per tool, or observation size. Without a baseline the later phases cannot prove they helped.

**Changes.**

- `src/browser_mcp/perf.py` (new):
  - `@timed_tool` context manager used inside `_public_tool` (`server.py:231-247`) that records `{ts, tool, wall_ms, harness_calls, result_chars, ok}` per tool invocation.
  - `harness_calls` is counted by wrapping the bound helpers module in a thin counting proxy (`_CountingHelpers(helpers)` that increments a thread-local counter on every attribute call). Install the proxy in `_browser_harness()` so every tool sees it transparently. Make sure `hasattr` checks still pass through (`__getattr__` delegation).
  - Sink: append JSON lines to `$BROWSER_MCP_PERF_LOG` if set, else `<workspace>/browser/perf/tools.jsonl` (reuse `screenshots.workspace_root()`). Off by default unless `BROWSER_MCP_PERF=1`. Never log tool arguments (playbooks and typed text may contain PII).
- `scripts/perf_bench.py` (new, under `integrations/browser-mcp/scripts/`): drives the MCP tools in-process (call `server.browser_goto(...)` etc. directly, same as tests) against three static fixtures served by `python -m http.server` from `tests/fixtures/`:
  - `form.html` — a 12-field form with labels, a `<select>`, and a React-style controlled input (a tiny inline script that re-renders `value` from state on `input` events).
  - `long_list.html` — 600 links and 200 buttons across 8 viewport heights.
  - `spa.html` — a hash-router page whose "Next" button swaps content after a 300 ms timeout and fires a `fetch` to a local endpoint.
  - The script runs a fixed scenario per fixture (goto → get_elements → fill 6 fields → click submit → get_elements) and prints a table: tool, median wall ms, harness calls, result chars, and total scenario time. Also prints "tool calls per scenario" — the proxy for model turns.
- `evals/`: add a browser scenario suite only if one already exists in `evals/suites`; otherwise defer. Turn-count per task is measured with the eval runner's existing tool-call count, not new code.

**Contracts.** None user-visible. Perf JSONL schema above is internal.

**Tests.** `tests/test_perf.py`: proxy counts calls; log is skipped when disabled; log line has the schema; arguments never appear in the log.

**Acceptance.** `perf_bench.py` runs green on macOS against a local Chrome (`BU_CDP_URL`) and prints the baseline table. Commit the baseline output to `docs/browser-mcp-perf-baseline.md`. **Met** — see that file (headed Chrome; headless hung on `fill_input` key events).

**Risks.** Counting proxy must not change semantics (`MagicMock` in tests will pass through fine).

---

### Phase 1 — Inject the driver once, do compound work in-page — **completed**

**Status.** Done. Driver persists across navigations; in-page fill/settle and in-place `browser_goto` are in tree.

**Goal.** Cut harness round trips per tool by 5–20× without touching browser-harness.

**Why.** `_ensure_driver` (`dom_indexing.py:54-70`) costs 1 check + 3 chunk pushes + 1 eval + 1 verify = 6 IPC calls on every fresh document. `browser_click_by_index` is 1 JS + 2 CDP calls. `browser_input_by_index` is 1 JS + `fill_input` (1 JS + 2 select-all + 3 backspace + 3 per char + 1 JS). Most of this can be one call.

**Changes.**

1. **Persistent driver registration** (`dom_indexing.py`):
   - New `_register_driver_for_new_documents(helpers, target_id)`. If `hasattr(helpers, "cdp")`: for each base64 chunk, call `helpers.cdp("Page.addScriptToEvaluateOnNewDocument", source=<small JS that pushes this chunk onto window.__bmcpChunks>)`, then one final registration whose source joins + `atob` + `(0,eval)` and deletes the chunk array. CDP guarantees registered scripts run in registration order. Guard every chunk script with `if (window.__bmcp) return;` so iframes and repeated registrations are harmless.
   - Track registration per target in a module dict `_registered_targets: set[str]` keyed by `helpers.current_tab()["targetId"]` (falls back to `page_info()["url"]` host when `current_tab` is unavailable). Phase 2 moves this flag into the per-tab registry entry. Re-register after `browser_switch_tab`, `browser_goto` that creates a new target, and on backend rebind (`_teardown_bound_backend` clears the set).
   - Keep the existing chunked `eval` path for the **current** document (registration only affects future navigations). Call it once immediately after registration.
   - Playwright backend: add `add_init_script(source)` to `playwright_helpers.py` wrapping `context.add_init_script`, marshalled like the others. Full 60 KB source is fine there (no line limit).
   - Wrap the driver source in a `try/catch` that stores `window.__bmcpInjectError` so a CSP failure is diagnosable; `get_elements` reports it in `error`.
2. **In-page fast fill** (`assets/pa_driver.js` + `dom_indexing.py`):
   - Add `window.__bmcp.fill(index, text, {clear})`. For `input`/`textarea`: focus; set the value through the prototype setter (`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, text)`; same for `HTMLTextAreaElement`) so React's value tracker sees the change; dispatch `InputEvent('input', {bubbles:true, inputType:'insertText', data:text})` and `Event('change', {bubbles:true})`. For `contenteditable`: focus, select all, `document.execCommand('insertText', false, text)`. Return `{ok, value: el.value ?? el.textContent, tagName}`.
   - `dom_indexing.fill(helpers, index, text, clear_first, mode)`: `mode="auto"` (default) calls the in-page fill and verifies the returned `value == text`. If the framework reverted it (value mismatch) or the element declares `data-bmcp-keys` / has an `autocomplete` widget (`role=combobox`, `aria-autocomplete`), fall back to the existing `helpers.fill_input` key-event path. `mode="keys"` forces the fallback; `mode="fast"` never falls back.
   - `browser_input_by_index` gains `mode: str = "auto"` and returns `{ok, index, tagName, mode_used}`.
3. **One-call click target** (`pa_driver.js`): `getRect` already scrolls into view and returns the center. Keep it, but add `getRects(indices)` for Phase 7 annotations, and make `getRect` return `{..., visible, obscuredBy}` using `document.elementFromPoint` so the server can report "element covered by overlay" instead of clicking through a modal backdrop.
4. **Settle primitive** (`pa_driver.js`): install a `MutationObserver` on `document` at driver load that updates `__bmcp.lastMutation = performance.now()` and a counter. Add `__bmcp.settle(quietMs=150, maxMs=1500)` returning a **Promise** that resolves `{quiet:true|false, mutations}` when no mutation for `quietMs` or at `maxMs`. `helpers.js` evaluates with `awaitPromise=True` (`helpers.py:113-118`), so this is a single IPC call regardless of how long it waits. If the document navigates mid-await, the eval errors with "context destroyed"; catch that in `dom_indexing.settle` and return `{quiet:true, navigated:true}`.
5. **`browser_goto` tab reuse** (`server.py:392-399`): it calls `helpers.new_tab(url)` every time, and `new_tab` only reuses the current tab when that tab is `about:blank` (`helpers.py:308-323`). So every navigation from a non-blank page creates a new target: `Target.createTarget` + `switch_tab` (activate, attach, `set_session` with four domain enables, two title-mark evals) + `goto_url`, roughly 9 IPC calls, and the old tab leaks. Change to: if `helpers.current_tab()` is a real page, `helpers.goto_url(url)` (1 call); else `new_tab`. Add `new_tab: bool = False` to `browser_goto` for callers that genuinely want a second tab.
6. **`wait_for_load`** in `browser_goto`: replace the 300 ms polling loop with one awaited in-page promise: `document.readyState==='complete' ? true : new Promise(r => addEventListener('load', () => r(true), {once:true}))` with a `setTimeout` race at `timeout`. Then `settle()`.

**Contracts.**
- `browser_input_by_index(index, text, clear_first=True, mode="auto")` → `{"ok": true, "index": n, "tagName": "input", "mode_used": "fast"|"keys"}`.
- `browser_click_by_index` unchanged shape; adds `"warning": "target obscured by <tag>"` when applicable and still clicks.

**Tests.**
- Unit: `_register_driver_for_new_documents` issues N+1 `cdp("Page.addScriptToEvaluateOnNewDocument", ...)` calls each under 60 000 bytes of JSON; registers once per target; re-registers after `browser_switch_tab`.
- Unit: `fill` auto mode falls back to `helpers.fill_input` on value mismatch; `mode="keys"` skips in-page fill.
- Integration (`BROWSER_MCP_INTEGRATION=1`): on `form.html`, the controlled input keeps its value after fast fill; the submit button becomes enabled; navigation to a second page still has `window.__bmcp` without re-injection (assert harness call count for `get_elements` after navigation is exactly 1).

**Acceptance (vs Phase 0 baseline).** **Met** — [Phase 1 numbers](browser-mcp-perf-baseline.md). `get_elements` after navigation 9 → 1; `input_by_index` wall 14.0 → 0.6 ms (1 harness call); `goto` 133 → 12 ms / 5 harness calls (`current_tab` + `goto_url` + load + settle + `page_info`; polling gone). Form scenario 255.9 → 22.3 ms (−91 %).

**Risks.** Sites with strict CSP block `eval` — same failure mode as today, now surfaced with an explicit error. `addScriptToEvaluateOnNewDocument` runs in every frame; the presence guard handles that. In-page fill can defeat sites that validate on `keydown`; the auto-fallback plus `mode="keys"` covers it, and the SKILL text must tell the model when to use it.

---

### Phase 2 — Multi-tab control — **completed**

**Goal.** Let the agent hold several tabs open and address any of them from any tool, so it can compare pages, keep a reference page while filling a form elsewhere, or fan out reads across N result pages in one call.

**Why.** Today the harness daemon holds exactly one active CDP session (`daemon.py:213-217`, `set_session` at `daemon.py:337-372`) and every `browser_*` tool acts on it. `browser_switch_tab` is the only way to reach another tab, and it costs ~6 IPC calls (activate, attach, `set_session` with four domain enables, two title-mark evals; `helpers.py:294-306`). The daemon *does* honor a per-request `session_id` for any non-`Target.*` method (`daemon.py:377-379`), and `helpers.cdp(method, session_id=..., **params)` exposes it, so the server can route reads to any tab without switching. Nothing surfaces this to the agent, and raw 32-hex target IDs are expensive and error-prone for a model to carry around.

**Design in one paragraph.** The server owns a **tab registry** keyed by target ID with a short alias per tab (`t1`, `t2`, … or a model-chosen name). Every tool takes `tab: str | None = None` (alias or target ID; `None` = the focused tab, which keeps the current behavior). **Reads run on any tab without changing focus**, routed by session ID. **Actions and screenshots focus the tab first** (one `switch_tab` only when the target differs from the current focus), because headed Chrome throttles timers and pauses rendering in background tabs, so clicking or capturing there is unreliable. The rule the model learns is: *reads never move focus, actions do.* The agent may control at most five tabs at once; past that, it asks the user which one to drop (item 8).

**Changes.**

1. **Tab registry** (`src/browser_mcp/tabs.py`, new):
   - `TabState` dataclass: `target_id, alias, session_id | None, driver_registered: bool, last_tree: list[str] | None, last_url: str | None, opened_by_agent: bool`.
   - `TabRegistry` with `refresh(helpers)` (reconcile against `helpers.list_tabs(include_chrome=False)`; new targets get the next `tN` alias, closed ones are dropped and their alias retired, never reused), `resolve(tab: str | None) -> TabState` (alias → target ID → error listing known aliases), `focused() -> TabState`, `set_alias(tab, alias)` (aliases must match `[a-z][a-z0-9_-]{0,23}` and be unique), and `session_for(helpers, tab)` which lazily calls `helpers.cdp("Target.attachToTarget", targetId=..., flatten=True)` once per tab, then `Runtime.enable` and `Page.enable` on that session, caches the session ID, and re-attaches when a call fails with "Session with given id not found". Detach (`Target.detachFromTarget`) on close and on backend teardown (`_teardown_bound_backend`, `server.py:288`).
   - The registry replaces the Phase 1 `_registered_targets` set and hosts the Phase 3 tree cache and the Phase 6 recent-actions ring, so all per-tab state has one owner.
2. **Session-routed primitives** (`src/browser_mcp/tabs.py`): a small `TabHandle` interface the tools use instead of calling helpers directly:
   - `evaluate(expr, await_promise=True)` → CDP backend: `helpers.cdp("Runtime.evaluate", session_id=sid, expression=expr, awaitPromise=True, returnByValue=True)` with the same value/exception decoding `helpers._runtime_value` does (reimplement locally; do not import private helpers). Playwright backend: `page.evaluate`.
   - `capture_screenshot(...)`, `navigate(url)`, `dispatch_mouse(...)`, `dispatch_key(...)`: CDP backend passes `session_id`; Playwright backend uses the page object.
   - `focused_handle()` delegates to the existing helpers so the focused-tab path is byte-for-byte what it is today (no behavior change when `tab=None`).
   - Phase 1's `_ensure_driver`, `fill`, `settle` and Phase 3's `get_elements` are written against `TabHandle`, so they work on any tab for free.
3. **Focus rule** in `server.py`: a `_for_action(tab)` helper resolves the tab and, if it is not the focused one, calls `helpers.switch_tab(target_id)` (CDP) or selects the page (Playwright) and updates `registry.focused`. A `_for_read(tab)` helper only resolves and returns the handle. Action tools (`click*`, `input*`, `select*`, `fill*`, `press_key`, `scroll`, `upload`, `act`, `fill_form`, `click_text`, `screenshot`, `run_playbook`) use `_for_action`; read tools (`get_elements`, `get_text`, `page_info`, `js`, `extract`, `wait_for`, `read_tabs`) use `_for_read`. `wait_idle` is focused-tab only (the daemon enables `Network` events only on the active session and `wait_for_network_idle` filters to it, `helpers.py:400-433`); on another tab it returns `{ok: true, idle: null, note: "network idle is only available on the focused tab; DOM settle was used"}` after a Phase 1 `settle`.
4. **New tools:**
   - `browser_open_tab(url, alias=None, focus=False)` → `Target.createTarget(url="about:blank", background=not focus)` then `navigate` via the handle (create-blank-then-navigate mirrors `helpers.new_tab`'s race note, `helpers.py:308-312`), register, optionally focus, wait for load, and return `{ok, tab: "t3", alias, url, title, focused}` plus a Phase 4 observation when `focus=True`. Playwright: `context.new_page()`.
   - `browser_close_tab(tab)` → `Target.closeTarget`; if it was focused, focus the most recently used remaining tab and say which. Refuses to close the last tab (navigates it to `about:blank` instead).
   - `browser_read_tabs(tabs: list[str] | None = None, mode="text", max_chars=3000)` → fan-out read over the listed tabs (default: all agent-opened tabs), returning `[{tab, alias, url, title, text | tree, truncated}]`. Sequential on the CDP backend (one IPC per tab, no focus changes); this is the tool for "open five results and compare".
   - `browser_tabs` (existing) now returns `[{tab, alias, url, title, focused, opened_by_agent}]` sorted with the focused tab first. `browser_switch_tab(tab)` accepts aliases and keeps its shape.
   - `browser_goto(url, tab=None, new_tab=False)`: `tab=None` navigates the focused tab in place (Phase 1 item 5); `new_tab=True` behaves like `browser_open_tab(url, focus=True)`.
5. **`browser_act` steps** (Phase 5): add `{"do":"tab","tab":"t2"}` and `{"do":"open_tab","url":u,"alias":a,"focus":true}` so a batch can move between tabs; the final observation is for whichever tab is focused at the end.
6. **Serialize tool execution.** FastMCP runs sync tools on a thread pool, and core may issue parallel tool calls; two tools racing on focus would interleave `switch_tab` calls. Add a module-level `threading.RLock` acquired inside `_public_tool` (`server.py:231-247`). Reads on different tabs are still cheap, so serialization costs little and removes a whole class of bugs.
7. **Instructions and SKILL.** Add a "Tabs" paragraph to the server instructions (`server.py:44-70`) and the three SKILL copies: aliases, the reads-don't-move-focus rule, when to open a second tab (comparison, keeping a form's state, reference material), `browser_read_tabs` for fan-out, the five-tab cap and what to do when it is hit (item 8). Tell the model to close tabs it opened when done.
8. **Five-tab cap, user decides what to drop.** The agent may control at most `BROWSER_MCP_MAX_TABS` tabs at once (default **5**; the count is tabs in the registry the agent has acted on or opened, not tabs the user opened and the agent never touched). When `browser_open_tab` or `browser_goto(new_tab=True)` would exceed the cap, the server does **not** open the tab and does **not** close one on its own. It returns:
   ```json
   {"ok": false, "error": "tab_limit_reached", "limit": 5,
    "tabs": [{"tab": "t1", "alias": "search", "url": "...", "title": "...", "last_used": "2026-09-04T18:02:11Z", "focused": true}, ...],
    "action_required": "Ask the user which tab to close (show them the list with aliases, titles, and last-used times), then call browser_close_tab(tab) with their choice and retry. Do not close a tab without the user's confirmation."}
   ```
   The registry records `last_used` per tab (updated on any tool call that addresses it) so the user can see which tabs are stale. The SKILL text tells the model to relay the list to the user verbatim, ask which one to drop, and only then call `browser_close_tab`. If the user says to drop several, the model closes each one it was told to. Closing the tab the user is actively looking at is allowed only when the user picked it. `browser_stop` still closes every agent-opened tab regardless of the cap, since the user asked for the session to end.

**Contracts.**
- Every existing tool gains `tab: str | None = None` as its **last** parameter (keyword use only); omitted ⇒ focused tab ⇒ identical behavior to today.
- `browser_tabs()` → `{"ok": true, "focused": "t1", "tabs": [{"tab": "t1", "alias": "t1", "url": ..., "title": ..., "focused": true, "opened_by_agent": false}, ...]}`.
- `browser_open_tab(url, alias=None, focus=False)` → `{"ok": true, "tab": "t2", "alias": "results", "url": ..., "title": ..., "focused": false}`.
- `browser_read_tabs(tabs=None, mode="text", max_chars=3000)` → `{"ok": true, "tabs": [{"tab": "t2", "url": ..., "title": ..., "text": ..., "truncated": false}, ...]}`.
- Errors: unknown tab → `{"ok": false, "error": "unknown tab 'foo'; known: t1 (focused), t2 (results)"}`; backend cannot create targets → `{"ok": false, "error": "this browser backend supports a single tab"}`; cap reached → the `tab_limit_reached` shape in item 8, which the model must turn into a question to the user.
- `browser_tabs()` entries also carry `last_used` (ISO-8601 UTC) so the model can show the user which tabs are stale.

**Tests.**
- Unit (`tests/test_tabs.py`): alias assignment and retirement across `refresh`; `resolve` accepts alias and raw ID; the sixth agent-controlled tab returns `tab_limit_reached` with all five tabs listed and no `Target.createTarget` call; user-opened tabs the agent never touched do not count toward the cap; `browser_close_tab` followed by a retry succeeds; `browser_stop` closes agent-opened tabs even at the cap; `session_for` attaches once and re-attaches on "Session with given id not found"; `_for_action` switches only when the target differs from focus; `_for_read` never calls `switch_tab`; `wait_idle` on a non-focused tab returns the DOM-settle note; the RLock serializes two concurrent tool calls (use a `MagicMock` helper that records interleaving).
- Integration (`BROWSER_MCP_INTEGRATION=1`): open `form.html` in `t1` and `long_list.html` in `t2` (background); `get_elements(tab="t2")` returns the list without changing the focused tab (assert `browser_tabs().focused == "t1"`); `click_by_index(..., tab="t2")` focuses `t2`; `read_tabs()` returns both; closing `t2` refocuses `t1`.
- Playwright backend: the same unit tests against `playwright_helpers` with a fake page map.

**Acceptance.** **Met** — background reads do not call `switch_tab`; the five-tab cap returns `tab_limit_reached` without creating or auto-closing; `perf_bench.py` `compare_three` is 4 tool calls (`goto` + 2× `open_tab` + `read_tabs`). Single-tab scenarios omit `tab`.

**Risks.**
- **Background throttling.** Headed Chrome throttles timers to 1 Hz and pauses rendering in background tabs, and freezes them after ~5 min. Reads (`Runtime.evaluate`, layout queries) are fine; anything that needs timers or paint is not, which is why actions focus first. Document this so the model does not expect a background SPA to finish loading.
- **In-app Spaces browser.** It is an Electron bridge, not Chrome; `Target.createTarget` may be unsupported or open a view the user cannot see. Detect by catching the create error and return the single-tab error above; never crash the server.
- **Event buffer is global.** `drain_events` empties the daemon's shared buffer (`daemon.py:305-307`), so anything reading events for one tab steals them from another. This phase deliberately keeps network-idle on the focused tab only; do not try to multiplex it without Phase 9's daemon changes.
- **Tab explosion.** The agent may open tabs and forget them. `browser_tabs` marks agent-opened tabs, `browser_stop` closes them, and the SKILL tells the model to close what it opened. The hard cap in item 8 (five agent-controlled tabs) bounds the damage, and the cap is a user decision, never an automatic close.

---

### Phase 3 — Observation diet — **completed**

**Status.** Done. Viewport-first default, stable indices, filters, diff mode, and `browser_get_text` are in tree. Acceptance: [Phase 3 numbers](browser-mcp-perf-baseline.md).

**Goal.** Cut tokens per observation by ≥ 60 % and make indices reusable across actions.

**Why.** Full-page trees are the largest tool results the model reads, and it re-reads them after every action. The tool instructions currently forbid reusing indices after any DOM change (`server.py:53-55`), forcing a refetch even when nothing relevant moved.

**Changes.**

1. **Stable indices** (`pa_driver.js`): compute `stableKey(el)` = hash of `tagName`, `id`, `name`, `role`, `aria-label`, `placeholder`, `type`, `href` (path only), first 40 chars of own text, and the `nth-of-type` path of up to 3 ancestors. Keep `window.__bmcpIndexByKey` (key → index) and `window.__bmcpNextIndex` for the lifetime of the document. On each `getTree`, reuse an element's previous index when its key is known, else assign the next unused index. Indices therefore never change for an element that survives a mutation, and are never recycled within a document. Update the `selectorMap` accordingly. Document the guarantee in tool docstrings and SKILL: *indices remain valid until navigation; after navigation call `get_elements` again*.
2. **Viewport first** (`server.py:404`): default `viewport_only=True`. Append a footer line to `tree`: `"… N more interactive elements below the viewport (scroll or pass viewport_only=false)"` computed by running the walker with `viewportExpansion:-1` only for counting (or cheaper: `document.querySelectorAll` of the walker's interactive selector set outside the viewport rect). Keep `full` available.
3. **Filters** on `browser_get_elements`: `kind: str | None` in `{"inputs","buttons","links","all"}` mapped to tag/role sets in-page; `contains: str | None` substring match (case-insensitive) on the rendered line, applied in-page before flattening so the `selectorMap` still includes everything; `max_elements: int = 150` truncating with the footer `"… truncated, K elements omitted; use contains= or scroll"`.
4. **Attribute trimming** (`pa_driver.js:12-17`): drop `id` and `name` when `aria-label`, `placeholder`, or text already identifies the element; cap attribute values at 20 chars (already) and text at 60 chars; collapse whitespace. Keep `href` only as the path segment.
5. **Diff mode** (`dom_indexing.py`): the server caches the last rendered tree per tab in the Phase 2 registry (`TabState.last_tree`). `browser_get_elements(observe="diff")` returns `{added: [...lines], removed: [...lines], unchanged: n, elementCount, url, title}` using `difflib.SequenceMatcher` on lines; because indices are stable, unchanged lines compare equal. Default remains `"full"` for the explicit tool; Phase 4 makes `"diff"` the default for post-action observations. Cache is invalidated on navigation (URL change or `navigated:true` from settle).
6. **`browser_get_text(max_chars=8000, selector=None)`** (new tool): readable page text for *reading*, separate from the interactive tree. In-page: prefer `<main>`/`<article>`/`[role=main]`, else `body`; strip `script/style/nav/footer/aside`; collapse whitespace; return `{text, truncated, url, title}`. Replaces today's ad-hoc `browser_js("document.body.innerText")` turns.

**Contracts.**
- `browser_get_elements(viewport_only=True, kind=None, contains=None, max_elements=150, observe="full")` → `{ok, url, title, elementCount, tree, truncated: bool, below_viewport: int}` or the diff shape above.
- `browser_get_text(max_chars=8000, selector=None)` → `{ok, text, truncated, url, title}`.

**Tests.**
- Integration on `long_list.html`: viewport default returns < 150 elements with a correct `below_viewport` count; `contains="Buy"` filters; index for the same button is identical across two `getTree` calls after inserting 50 nodes above it.
- Unit: diff computation on synthetic line lists; cache invalidation on URL change.

**Acceptance.** **Met** — [Phase 3 numbers](browser-mcp-perf-baseline.md). Median `result_chars` of `get_elements` on `long_list.html` 21073 → 4889 (−77 %). Index stability test passes; `get_text` on `article.html` returns body text only.

**Risks.** Stable keys can collide for identical sibling elements (e.g. a list of identical "Add" buttons); the ancestor `nth-of-type` path disambiguates most, and a collision only means both keep distinct indices via the "next unused" rule (keys are hashed per element instance in a `WeakMap` fallback). Viewport-first can hide the element the model needs; the footer and `viewport_only=false` cover it, and the SKILL must say so.

---

### Phase 4 — Act then observe — **completed**

**Status.** Done. Actions return a settled observation (diff by default). Acceptance: [Phase 4 numbers](browser-mcp-perf-baseline.md).

**Goal.** Halve model turns: every action returns the resulting observation.

**Why.** Today each action is followed by a separate `get_elements` turn (`server.py:53-55`). The action tool already knows when the page settled (Phase 1) and can return the diff (Phase 3) for near-zero extra cost.

**Changes.**

1. New helper `server._observe_after(helpers, target_id, observe: str, action: dict, before_url: str)`:
   - `settle(quiet=150, max=1500)`; if a navigation happened, run the awaited load wait from Phase 1 and re-inject/verify the driver.
   - `observe="diff"` → Phase 3 diff (or full tree when there is no cache or navigation happened, with `"mode": "full"`). `observe="full"` → full viewport tree. `observe="none"` → today's response.
   - Return `{ok, action, page: {url, title, navigated}, observation: {...}}`.
2. Add `observe: str = "diff"` to `browser_click_by_index`, `browser_input_by_index`, `browser_select_by_index`, `browser_click`, `browser_fill`, `browser_press_key`, `browser_scroll`, `browser_switch_tab`. `browser_goto` returns `observe="full"` by default (new document ⇒ no meaningful diff).
3. Server instructions (`server.py:44-70`) and all three SKILL copies: rewrite the workflow to *"act; read the observation in the response; only call `browser_get_elements` when you need the whole tree or a different filter"*.
4. Timing budget: the settle wait is the only added latency. Expose `BROWSER_MCP_SETTLE_MS` (default 1500) and `BROWSER_MCP_QUIET_MS` (default 150).

**Contracts.**
```json
{"ok": true,
 "action": {"type": "click", "index": 12, "clicked": {"x": 300, "y": 220}},
 "page": {"url": "...", "title": "...", "navigated": false},
 "observation": {"mode": "diff", "added": ["[41]<div role=dialog>..."], "removed": [], "unchanged": 88, "elementCount": 91}}
```
Backward compatibility: the old top-level keys (`clicked`, `index`, `tagName`, `selected`) are kept alongside `action` for one release, then removed in a later major bump.

**Tests.**
- Unit with `MagicMock`: each action tool calls settle once and includes `observation` when `observe="diff"`; `observe="none"` yields the legacy shape.
- Integration on `spa.html`: clicking "Next" returns a diff containing the new content lines without any explicit wait call.

**Acceptance.** In `perf_bench.py` scenarios, tool calls per scenario down ≥ 45 % versus the Phase 0 baseline (the `get_elements` calls disappear). Eval turn counts on the browser smoke scenario down accordingly.

**Risks.** Diff after a big SPA transition may be larger than a fresh viewport tree; when `len(added)+len(removed) > 0.6 * full`, return `mode:"full"` instead. Some pages never go quiet (carousels, live tickers); the `maxMs` cap bounds it and the response says `settled:false`.

---

### Phase 5 — Batch and intent-level tools

**Goal.** Multi-step interactions (forms, login, pagination) in one turn.

**Why.** Even with act-then-observe, a six-field form is six turns. The model usually knows all values up front.

**Changes.**

1. **`browser_act(steps: list[dict], observe="diff", stop_on_error=True)`** (new). Step schema (validate with explicit checks, return `{ok:false, error, step_index}` on malformed input):
   - `{"do":"click","index":n}` · `{"do":"input","index":n,"text":s,"mode":"auto"}` · `{"do":"select","index":n,"text":s}` · `{"do":"press","key":"Enter","modifiers":0}` · `{"do":"click_text","text":s,"role":r?,"exact":false,"nth":0}` · `{"do":"wait_for","selector":s,"timeout":10}` · `{"do":"wait_idle","timeout":10}` · `{"do":"goto","url":u}` · `{"do":"scroll","dy":600}` · `{"do":"settle"}`.
   - Executes sequentially through the same code paths as the single tools (refactor each tool body into `_do_click(helpers, index)` etc. so both share one implementation). After each step run `settle`. Stop on the first failure and return `{ok:false, completed: [...per-step results], failed_step, error, observation}` so the model can resume from a known state. On success return `{ok:true, steps: [...], observation}` with one observation at the end.
   - Cap at 25 steps per call.
2. **`browser_fill_form(fields: dict[str,str], submit: bool=False, mode="auto")`** (new). In-page resolver `__bmcp.resolveField(label)` tries, in order: `<label for=id>` text, `aria-label`, `aria-labelledby`, `placeholder`, `name`, `id`, then nearest preceding visible text within the same form row; match is case-insensitive, whitespace-collapsed, and accepts substring when unique. Returns `{index, how}`. The server fills each resolved field with Phase 1 `fill`, selects for `<select>`, checks/unchecks for checkboxes when the value is `"true"/"false"`. `submit=True` presses Enter in the last field, or clicks the form's submit button if one exists. Response lists `unresolved: [labels]` and `filled: [{label, index, how}]`; unresolved labels are not an error unless all failed.
3. **`browser_click_text(text, role=None, exact=False, nth=0)`** (new). In-page: candidates are interactive elements (walker's set) whose visible text or `aria-label` matches; `role` restricts to `button|link|tab|menuitem|checkbox|radio|option`; `exact` toggles equality vs substring; prefer visible, in-viewport, and top elements; scroll into view and click via `click_at_xy`. Error lists the top 5 near-misses (`"did you mean"`), so the model does not need another `get_elements`.
4. **`browser_extract(selector, fields: dict[str,str], limit=50)`** (new, optional in this phase). For each element matching `selector`, evaluate each field's sub-selector (`"title": "h2"`, `"price": ".price"`, `"href": "a@href"` attribute syntax) and return rows. Replaces bespoke `browser_js` scraping turns.

**Contracts.** As above. All new tools use `_public_tool` and return `{"ok": ...}` JSON.

**Tests.**
- Unit: `browser_act` validation, stop-on-error, shared implementation used by single tools (assert the refactored `_do_*` functions are called).
- Integration on `form.html`: `fill_form` resolves all 12 labels via distinct strategies; a deliberately unlabeled field lands in `unresolved`; `click_text("Submit", role="button")` clicks the right one of two "Submit" texts.

**Acceptance.** Form scenario in `perf_bench.py`: tool calls 9 → 2 (`goto`, `fill_form`).

**Risks.** Label resolution heuristics can pick the wrong field on dense forms; the response's `how` field and `unresolved` list give the model a way to correct with an index. Keep resolver deterministic and documented in the tool docstring.

---

### Phase 6 — Executable playbooks

**Goal.** Repeat visits to a known site run with zero model turns until something diverges.

**Why.** Playbooks today are free text (`playbooks.py`). The model still has to read them and re-plan every time. Encoding proven flows as steps lets the server execute them directly with `browser_act`.

**Changes.**

1. **Format.** Keep `<host>.md` as the file; add optional fenced blocks:
   ````
   ```playbook
   name: login
   params: [username]
   steps:
     - {do: goto, url: "https://example.com/login"}
     - {do: fill_form, fields: {Email: "{{username}}"}}
     - {do: click_text, text: "Continue", role: button}
     - {do: wait_for, selector: "input[type=password]"}
   expect:
     url_contains: "/dashboard"
   ```
   ````
   `playbooks.py` gains `parse_flows(markdown) -> list[Flow]` (YAML inside ```playbook fences; use `yaml.safe_load`; add `pyyaml` to dependencies) and `render_flow(flow) -> str`. Unknown `do` values fail parse with a clear error. Secrets are never parameters: a flow that needs a password uses `{do: login, expected_origin: ...}` which maps to `browser_login` (`server.py:703`).
2. **`browser_run_playbook(host, name, params: dict = {}, observe="diff")`** (new): substitutes `{{param}}` (only into string fields; refuse unknown params), runs the steps via the Phase 5 executor, checks `expect` (`url_contains`, `selector`, `text`), and returns `{ok, name, completed, failed_step?, error?, observation}`. Cap total runtime with `BROWSER_MCP_PLAYBOOK_TIMEOUT_S` (default 120).
3. **Discovery.** `browser_list_playbooks` returns `{files: [...], flows: [{host, name, params}]}`. `browser_goto` already returns matching playbook file names (`server.py:398`); add `flows` there too so the model sees runnable flows in the first response.
4. **Recording aid.** The server keeps a ring buffer of the last 50 successful actions per host (type, index → resolved text/label, text lengths not contents). `browser_recent_actions(host)` returns them so the model can draft a flow from what actually worked. `browser_write_playbook` validates any ```playbook fences before saving and returns parse errors instead of writing a broken file.
5. **Divergence.** When a step fails, the response includes the Phase 3 observation and the failed step; the model continues by hand and is told (in SKILL) to append a corrected flow with `append: true`.

**Contracts.** As above. Flow YAML schema is documented in `docs/browser-mcp.md` under a new "Executable playbooks" section.

**Tests.**
- Unit: parse/render round trip; unknown `do`; param substitution refuses unknown names; password-looking params (`password`, `secret`, `token`) are rejected at parse time.
- Integration: a flow against `form.html` runs end to end; a flow with a stale selector returns `failed_step` and an observation.

**Acceptance.** Second run of the login scenario against the fixture: 1 tool call.

**Risks.** Playbooks are workspace data written by the agent; treat them as untrusted input (no shell, no file paths, no arbitrary JS steps — do **not** add a `js` step type).

---

### Phase 7 — Cheaper visual fallback — **7a completed**

**Status.** 7a done (JPEG default, `max_dim=1200`, `annotate`, `bytes`/`format`). 7b (inline MCP images) deferred.

**Goal.** When a screenshot is unavoidable, make it cheap and make it actionable in one step.

**Changes.**

1. `browser_screenshot(full=False, max_dim=1200, format="jpeg", quality=60, annotate=False)` (`server.py:467-498`): JPEG default via PIL (`Image.convert("RGB").save(..., quality=...)`); PNG remains available. `max_dim` default 1800 → 1200.
2. `annotate=True`: fetch rects for the current tree via `__bmcp.getRects()` (Phase 1) and draw index labels with PIL (small filled rectangle + index text at the element's top-left, contrasting colors), so the model can go from image straight to `browser_click_by_index`. Note in the response that labels correspond to the current indices.
3. Response gains `"bytes"` and `"format"` so the model can judge cost.
4. **7b (core change, optional):** allow MCP tools to return images inline. In `core/mcp/mcp_client.py` result flattening, detect `ImageContent` blocks and surface them; in `core_tool_executor.py` MCP dispatch, convert them with `_media_result` (`:1194`); add the browser screenshot tool to `_IMAGE_CAPABLE_TOOLS` semantics (or generalize the set to any tool that returned an `Image` block). Then `browser_screenshot(inline=True)` returns `mcp.server.fastmcp.Image` (available in `mcp` 1.27.1) and the `load_file` turn disappears. Ship 7b as a separate PR touching core, with its own tests under `tests/` for the executor path.

**Tests.** Unit: JPEG path writes a JPEG under the size cap; annotate draws N labels (assert via PIL pixel sampling on a synthetic rect list). Core PR: an MCP result with one text and one image block yields a text block and an `Image` block.

**Acceptance.** **7a met** — JPEG default on `long_list.html` is ≥ 70 % smaller than PNG `max_dim=1800`; see [Phase 7 numbers](browser-mcp-perf-baseline.md). 7b (screenshot-to-click in one turn, no `load_file`) is deferred.

---

### Phase 8 — Event-driven waits — **completed**

**Status.** Done. `browser_wait_for` uses an in-page MutationObserver (4s IPC chunks); `browser_wait_idle` is network idle then DOM settle. Item 4 (post-action network-aware settle) waits for Phase 4 `_observe_after`.

**Goal.** Remove polling from every remaining wait.

**Changes.**

1. `browser_wait_for(selector, visible, timeout)` (`server.py:553`): replace `helpers.wait_for_element` polling with one awaited in-page promise using a `MutationObserver` that resolves when `querySelector(selector)` matches (and passes `checkVisibility` when `visible`), racing a `setTimeout(timeout)`. One IPC call instead of `timeout/0.3`.
2. `browser_wait_idle`: keep `helpers.wait_for_network_idle` on the CDP backend (it already uses `drain_events`, `helpers.py:400-433`) but lower its poll interval by passing through `idle_ms`; on the Playwright backend use `page.wait_for_load_state("networkidle")`. Then combine with Phase 1 `settle` so "idle" means both network and DOM quiet.
3. `browser_goto`'s load wait was replaced in Phase 1; delete the `wait_for_load` polling call there.
4. Post-action `settle` (Phase 4) additionally consumes `Network.*` events from `drain_events` when available so a click that triggers a fetch is not reported settled before the response lands (bounded by `maxMs`).

**Tests.** Integration on `spa.html`: `wait_for("#page-2")` returns in ≈300 ms (element appears after the timeout) with exactly 1 harness call.

**Acceptance.** **Met** — [Phase 8 numbers](browser-mcp-perf-baseline.md). `wait_for("#page-2")` on `spa.html` is 1 harness call / ~304 ms (the fixture's 300 ms delay).

---

### Phase 9 — Harness IPC (upstream, optional)

**Goal.** Remove the per-request socket connect and the 64 KiB line limit at the source.

**Why.** After Phases 1–8 the remaining overhead is one connect per helper call (~0.3–1 ms locally, far more on remote daemons such as Browser Use Cloud where the daemon may be reached over TCP).

**Strategy.** browser-harness is external. Two options; pick one explicitly in the PR description:
- **A. Upstream PR** to `browser-use/browser-harness`, then bump the pin. Preferred if maintainers accept it.
- **B. Vendored fork** at `integrations/browser-harness/` installed as a path dependency. Only if A stalls. Keep the diff minimal and rebase-friendly.

**Changes (in browser-harness).**
1. `daemon.serve` handler (`daemon.py:389-402`): loop over `reader.readline()` until EOF, handling many requests per connection. Pass `limit=1 << 20` to `asyncio.start_unix_server` / `start_server` in `_ipc.serve`. Advertise capabilities in the ping reply: `{"pong": true, "caps": ["multi", "batch", "limit_1m"]}`.
2. `handle`: new `{"meta": "batch", "requests": [...]}` executing sequentially in one event-loop turn and returning `{"results": [...]}`, so `click_at_xy` (2 CDP calls) and `press_key` (2–3) become one round trip.
3. `helpers._send` (`helpers.py:43-50`): keep a thread-local persistent socket; reconnect on `BrokenPipe`/`ConnectionReset`; fall back to connect-per-request if the ping `caps` lacks `multi` (old daemons keep working). Add `helpers.batch(requests)`.
4. `fill_input`, `press_key`, `click_at_xy` use `batch` when available.

**Changes (in browser-mcp).** Feature-detect `helpers.batch`; nothing else changes because Phases 1–8 already minimized the call count.

**Tests.** Upstream tests for multi-request connections and batch; browser-mcp unit test that `_do_click` uses `batch` when present.

**Acceptance.** `perf_bench.py` wall time per tool down a further ≥ 30 % on the local daemon; measurable but larger gains on a remote daemon.

**Risks.** A persistent client socket must be per-thread (FastMCP dispatches sync tools on a pool); a shared socket would interleave requests. Windows TCP path needs the token on every request (already handled in `ipc.request`).

---

## Part 3 — Cross-cutting

### Rollout and flags

- Every behavior change that alters default output has an env override for one release: `BROWSER_MCP_VIEWPORT_DEFAULT` (`1`), `BROWSER_MCP_OBSERVE_DEFAULT` (`diff`), `BROWSER_MCP_FILL_MODE` (`auto`), `BROWSER_MCP_SETTLE_MS`, `BROWSER_MCP_QUIET_MS`, `BROWSER_MCP_MAX_TABS` (`5`), `BROWSER_MCP_PERF`.
- **Do not bump the package version per phase.** `0.5.0` is the single minor for this entire performance work. Append each phase's contract additions to `CHANGELOG.md` under that release (Unreleased until publish).
- Docs: update `docs/browser-mcp.md` "Tools" section per phase and add "Executable playbooks" (Phase 6) and "Performance tuning" (env vars) sections. Update the three SKILL copies' workflow text per Constraint 6.

### Test matrix

| Layer | Where | Runs in CI | Needs |
|---|---|---|---|
| Unit (mock helpers) | `tests/test_*.py` | yes | nothing |
| JS driver + tools | `tests/integration/` | only with `BROWSER_MCP_INTEGRATION=1` | Chrome via Playwright, `tests/fixtures/*.html` |
| Perf bench | `scripts/perf_bench.py` | manual | local Chrome (`BU_CDP_URL`) |
| Agent-level turns | `evals/` browser scenario | manual/live | full harness |

### Definition of done for the whole plan

| Metric (form scenario, `perf_bench.py`) | Baseline | Target |
|---|---|---|
| Tool calls per scenario | 11 | −70 % |
| Median chars per observation | 1301 (form) / 21073 (long_list) | −60 % |
| Harness calls per `input_by_index` (helpers API; 10-char `fill_input`) | 2 (internal CDP is in wall_ms ≈ 14) | ≤ 2 |
| Harness calls per `get_elements` after navigation | 9 (long_list) | 1 |
| Harness calls per `wait_for` | timeout/0.3 | 1 |
| Screenshot bytes | not measured in Phase 0 bench | −70 % |
| Harness calls to read a background tab | n/a (unsupported) | 1, no focus change |

### Suggested order and sizing

| Phase | Size | Depends on |
|---|---|---|
| 0 Instrumentation (completed) | S | — |
| 1 Driver once + in-page fill/settle (completed) | M | 0 |
| 2 Multi-tab control (completed) | M | 1 |
| 3 Observation diet (completed) | M | 1, 2 (settle, driver, registry) |
| 4 Act then observe | S | 1, 3 |
| 5 Batch + intent tools | M | 4 |
| 6 Executable playbooks | M | 5 |
| 7 Visual fallback (7b touches core) | S / M | 1 |
| 8 Event-driven waits | S | 1 |
| 9 Harness IPC | M, external | — (independent) |

Phases 7, 8, and 9 can proceed in parallel with 5 and 6 once Phases 1 and 2 have landed.
