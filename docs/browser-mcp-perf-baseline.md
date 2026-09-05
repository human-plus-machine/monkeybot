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

