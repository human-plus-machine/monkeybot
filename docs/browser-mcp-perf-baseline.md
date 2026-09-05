# Browser MCP — Phase 0 performance baseline

Recorded **2026-09-04** on macOS (darwin 25.6.0), Chrome 152.0.7977.82 headed
(`BU_CDP_URL=http://127.0.0.1:9333`), `monkeybot-browser-mcp` 0.5.0.
Command: `BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:9333 uv run python scripts/perf_bench.py`
from `integrations/browser-mcp/`. Three runs per scenario; table values are medians.

`harness_calls` counts public `helpers.*` invocations seen by the tool (the
`CountingHelpers` proxy). Internal CDP chatter inside `helpers.fill_input` is
**not** included — that cost shows up in `wall_ms`. Headless Chrome hung on
`Input.dispatchKeyEvent` during `fill_input`; headed Chrome was used instead.

Form scenario: `goto` → `get_elements` → fill 6 fields via `browser_input_by_index`
→ `get_elements` (Submit starts disabled until Nickname is filled, so it is
unindexed until then) → `click_by_index(Submit)` → `get_elements`. 11 tool
calls is the model-turn proxy for this fixture.

---

### form.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 133.1 | 3.0 | 148 |
| browser_get_elements | 1.6 | 2.0 | 1301 |
| browser_input_by_index | 14.0 | 2.0 | 44 |
| browser_click_by_index | 5.3 | 2.0 | 117 |

- tool_calls_per_scenario: **11**
- total_scenario_ms (median of 3): **255.9**

`get_elements` median harness_calls is 2 because two of the three calls per run
hit an already-injected driver. Use **long_list.html** for the after-navigation
inject cost.

### long_list.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 84.1 | 3.0 | 152 |
| browser_get_elements | 14.3 | 9.0 | 21073 |

- tool_calls_per_scenario: **2**
- total_scenario_ms (median of 3): **97.9**

This is the after-navigation observation to beat: **9 harness calls**, **~21k
result chars**.

### spa.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 29.4 | 3.0 | 147 |
| browser_get_elements | 4.8 | 5.5 | 170 |
| browser_click_by_index | 2.1 | 2.0 | 115 |

- tool_calls_per_scenario: **4**
- total_scenario_ms (median of 3): **42.0**

---

Later phases quote acceptance against these numbers (form tool-calls 11, long_list
`get_elements` 9 harness calls / 21073 chars, `input_by_index` wall ~14 ms for a
10-char `fill_input`).

---

## Phase 1 (2026-09-04)

Same machine and Chrome 152 headed, `BU_CDP_URL=http://127.0.0.1:9334`.
Command: `BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:9334 uv run python scripts/perf_bench.py`
from `integrations/browser-mcp/`. Three runs per scenario; table values are medians.

### form.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 12.0 | 5.0 | 148 |
| browser_get_elements | 0.8 | 1.0 | 1301 |
| browser_input_by_index | 0.6 | 1.0 | 65 |
| browser_click_by_index | 2.4 | 2.0 | 154 |

- tool_calls_per_scenario: **11** (unchanged; act-then-observe is Phase 4)
- total_scenario_ms (median of 3): **22.3** (−91 % vs Phase 0 255.9)

### long_list.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 12.9 | 5.0 | 152 |
| browser_get_elements | 6.1 | 1.0 | 21073 |

- tool_calls_per_scenario: **2**
- total_scenario_ms (median of 3): **20.2**

After-navigation `get_elements`: **9 → 1** harness calls.

### spa.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 9.3 | 5.0 | 147 |
| browser_get_elements | 0.7 | 1.0 | 170 |
| browser_click_by_index | 1.5 | 2.0 | 152 |

- tool_calls_per_scenario: **4**
- total_scenario_ms (median of 3): **13.4**

---

## Phase 2 (2026-09-05)

Same machine and Chrome 152 headed, `BU_CDP_URL=http://127.0.0.1:9334`.
Command: `BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:9334 uv run python scripts/perf_bench.py`
from `integrations/browser-mcp/`. Three runs per scenario; table values are medians.

`compare_three` is `goto(form.html)` → `open_tab(long_list.html)` →
`open_tab(spa.html)` → `read_tabs()` = **4 tool calls**. Extra tabs from prior
runs are closed via internals so they do not inflate the perf log or hit the
five-tab cap. Single-tab scenarios omit `tab=` and keep the Phase 1 shape.

### compare_three

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 10.8 | 5.0 | 146 |
| browser_open_tab | 34.9 | 25.0 | 126 |
| browser_read_tabs | 4.9 | 10.0 | 3304 |

- tool_calls_per_scenario: **4**
- total_scenario_ms (median of 3): **95.6**
- Background `get_elements(tab=...)` does not call `switch_tab`.

`open_tab` harness_calls includes create/attach/enable/navigate/page_info (two
opens per run; table is the median of those six samples). `read_tabs` is
sequential session evaluates with no `switch_tab`.

### Single-tab scenarios (no `tab=`) vs Phase 1

| scenario | tool_calls | total_ms Phase 1 | total_ms Phase 2 | get_elements harness_calls |
|---|---:|---:|---:|---:|
| form.html | 11 | 22.3 | 27.4 | 1.0 |
| long_list.html | 2 | 20.2 | 20.7 | 1.0 |
| spa.html | 4 | 13.4 | 14.6 | 1.0 |

Tool-call counts and per-tool harness_calls match Phase 1. Wall-time deltas are
noise (same headed Chrome, new session).

---

## Phase 7a (2026-09-05)

Same machine, Chrome 152 headed on `BU_CDP_URL=http://127.0.0.1:9222`.
Command: `BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:9222 uv run python scripts/perf_bench.py`
from `integrations/browser-mcp/`. Three runs; table values are medians. The new
`screenshot (long_list.html)` scenario is `goto` → `get_elements` →
`screenshot(format="png", max_dim=1800)` → `screenshot()` (JPEG defaults).

### screenshot (long_list.html)

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 43.5 | 5.0 | 152 |
| browser_get_elements | 24.1 | 1.0 | 21073 |
| browser_screenshot | 99.6 | 2.0 | 485 |

- tool_calls_per_scenario: **4**
- total_scenario_ms (median of 3): **282.0**
- screenshot_png_bytes (max_dim=1800): **156343**
- screenshot_jpeg_bytes (max_dim=1200, q=60): **40486** (−74 % vs PNG 1800)

JPEG default meets the ≥ 70 % size cut. Tool JSON stays metadata-only (`result_chars` ~485); image bytes are the file on disk, not the tool result. 7b (inline MCP images) is deferred.

---

## Phase 8 (2026-09-05)

Playwright Chromium 151 headless (same stack as `BROWSER_MCP_INTEGRATION=1`).
`spa_wait` scenario: `goto spa.html` → `get_elements` → `click_by_index(Next)` → `browser_wait_for("#page-2")`.
Three runs; table values are medians. `#page-2` is injected after a 300 ms timeout plus a local `fetch`.

### spa_wait

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_wait_for | 304.4 | 1.0 | 27 |

- tool_calls_per_scenario: **4**
- `wait_for("#page-2")` harness calls: **1** (was ≈ timeout/0.3 with `helpers.wait_for_element`)
- wall time matches the fixture delay (~300 ms), not a 300 ms poll loop

Acceptance met. `browser_wait_idle` now runs network idle then DOM `settle` (not in this bench).

---

## Phase 3 (2026-09-05)

Playwright Chromium 151 headless (same stack as `BROWSER_MCP_INTEGRATION=1`),
`BU_CDP_URL=http://127.0.0.1:9335`. Command:
`BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:9335 uv run python scripts/perf_bench.py`
from `integrations/browser-mcp/`. Three runs per scenario; table values are medians.

`browser_get_elements()` now defaults to the viewport (`max_elements=150`) with
stable indices. The form scenario still passes `viewport_only=False` so Nickname
and Submit (below a typical viewport) stay addressable; long_list uses the new
default and is the observation-size target.

### long_list.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 9.4 | 5.0 | 150 |
| browser_get_elements | 10.0 | 1.0 | 4889 |

- tool_calls_per_scenario: **2**
- total_scenario_ms (median of 3): **18.7**
- `get_elements` result_chars: **21073 → 4889 (−77 %)** vs Phase 0

### form.html (full tree; `viewport_only=False`)

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 6.6 | 5.0 | 146 |
| browser_get_elements | 1.1 | 1.0 | 1062 |
| browser_input_by_index | 0.4 | 1.0 | 65 |
| browser_click_by_index | 3.1 | 2.0 | 140 |

- tool_calls_per_scenario: **11** (unchanged; act-then-observe is Phase 4)
- total_scenario_ms (median of 3): **16.3**
- Attribute trimming cut full-form `get_elements` 1301 → 1062 chars.

Acceptance met (≥ 70 % char cut on long_list, stable indices, `browser_get_text` on `article.html`).

---

## Phase 4 (2026-09-05)

Playwright Chromium 151 headless (same stack as `BROWSER_MCP_INTEGRATION=1`),
`BU_CDP_URL=http://127.0.0.1:64471`. Command:
`BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:64471 uv run python scripts/perf_bench.py`
from `integrations/browser-mcp/`. Three runs per scenario; table values are medians.

Actions now settle and return an observation, so wall time per tool includes the
quiet window (~150 ms) and, for clicks, a retry until the tree changes. Tool-call
count is the model-turn proxy.

### form.html (full tree; `viewport_only=False`)

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 329.8 | 8.0 | 1395 |
| browser_get_elements | 2.1 | 1.0 | 1035 |
| browser_input_by_index | 160.4 | 5.0 | 471 |
| browser_click_by_index | 163.8 | 6.0 | 664 |

- tool_calls_per_scenario: **9** (was 11; dropped the post-fill and post-click `get_elements`)
- total_scenario_ms (median of 3): **1458.8**

Nickname/Submit sit below a typical viewport, so the scenario still starts with
one full-tree `get_elements`. The last fill observation supplies Submit; the
click observation replaces a trailing `get_elements`.

### spa.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 324.4 | 8.0 | 554 |
| browser_click_by_index | 469.1 | 9.0 | 740 |

- tool_calls_per_scenario: **2** (was 4; −50 %)
- total_scenario_ms (median of 3): **794.7**

`goto` returns the page-1 tree; `click_by_index(Next)` retries settle until the
300 ms SPA update lands, then returns the page-2 diff (`Page 2 done`). No
follow-up `get_elements` or `wait_for`.

Acceptance met (spa tool-calls 4 → 2, form 11 → 9).

---

## Phase 5 (2026-09-05)

Playwright Chromium headless on the same machine (fast fill; no `fill_input` key-event hang). Form scenario is `goto` + `browser_fill_form(..., submit=True)` for the six labeled bench fields including Nickname.

### form.html

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_goto | 330.5 | 9.0 | 1226 |
| browser_fill_form | 172.7 | 18.0 | 1125 |

- tool_calls_per_scenario: **2** (Phase 0: 11; Phase 4: 9; −82 % vs Phase 0)
- total_scenario_ms (median of 3): **503.9**

Acceptance met (form tool-calls 9 → 2).

---

## Phase 6 (2026-09-05)

Playwright Chromium 151 headless (same stack as `BROWSER_MCP_INTEGRATION=1`).
The `form_playbook` scenario writes a signup flow once, then each timed run is
only `browser_run_playbook("127.0.0.1", "signup")` against `form.html`.

### form_playbook

| tool | median_wall_ms | harness_calls | result_chars |
|---|---:|---:|---:|
| browser_run_playbook | 768.7 | 45.0 | 1467 |

- tool_calls_per_scenario: **1** (Phase 0: 11; Phase 5: 2)
- First-run wall ms: **768.7** (goto + fill_form + expect + observation inside one tool)

Acceptance met (second visit is 1 tool call).

