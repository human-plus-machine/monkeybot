# Dynamic spill and read budgets

Status: **implemented** (see [Implementation sketch](#implementation-sketch) checkboxes)  
Scope: window-derived spill thresholds, a unified `read_file` char budget (the two are inseparable — spill paging is worthless if reads stay capped at 32k), and a single configuration rule for both ([derived or YAML, never env](#configuration-surface)).  
Related: `core/tools/spill_inventory.py`, `core/tools/core_tool_executor.py`, `core/tools/workspace_service.py`, `core/runtime/context_budget.py`, `core/context/tool_result_ingress.py`, `core/context/tool_output_policy.py`, `core/context/__init__.py` (`read_schema`)

---

## Problem

Spill today uses fixed absolute knobs that ignore model context size:

| Knob | Default | Issue |
|------|---------|--------|
| `tools.spill_min_chars` / `MONKEYBOT_SPILL_MIN_CHARS` | `8000` | A result of ~10k chars forces a spill + `read_file` round-trip even on 200k–1M windows |
| `tools.spill_read_max_lines` / `MONKEYBOT_SPILL_READ_MAX_LINES` | `50000` | Not a cap on returned content — it is the ceiling the model's `limit` **argument** is validated against (`read_file` raises `invalid_limit` above it). Permissive, and the wrong unit |
| `MONKEYBOT_TOOL_RESULT_MAX_CHARS` (on `read_file`) | `32768` | `read_file` / `load_file` skip spill, so **every** read — spill path or ordinary source file — hits a flat 32k char chop applied *after* line selection. The payload still reports the line range it selected, so `end_line` lies (see below) |
| `tools.read_default_lines` / `MONKEYBOT_READ_DEFAULT_LINES` | `2000` | The **actual** binding limit on any read when the model omits `limit` |

Modern models range ~128k–1M tokens. The harness already tracks the window on
`TurnContext.context_window_tokens` and runs a dynamic `ContextBudgeter` later in
the turn — but spill decides size **before** that, with static chars.

### Current pipeline

1. Tool succeeds → if `len(result) >= 8000` and tool ∉ `{read_file, load_file}` → write full file, replace history text with inventory note + short preview.
2. Else → sanitize → hard-cap at `TOOL_RESULT_MAX_CHARS`.
3. Later → `ContextBudgeter` trims tool results vs remaining window / pressure tier, and applies `tool_output_policy` line/array caps.

Step 1 does not know the window. Step 2 undoes “read as much as you want” for
spill paging. Step 3 shapes **unconditionally** when a per-tool policy exists
(see [Interaction with `ContextBudgeter`](#interaction-with-contextbudgeter)) —
which would silently negate any inline budget this design grants.

### The `read_file` paging bug (pre-existing)

Step 2 chops the finished JSON by chars, but `read_file` selected its content by
**lines** and reports `start_line` / `end_line` / `truncated` describing that line
range. A 2000-line read of a large file is cut at 32k chars — roughly line 800 —
while still reporting `end_line: 2000`. The model then pages from 2001 and
silently skips ~1200 lines it was told it had seen.

This is not a spill concern; it affects every large read of an ordinary source
file, and it is the reason this design unifies the read path rather than carving
out spill paths (see [`read_file` budgets](#read_file-budgets-all-paths)).

---

## Goals

1. Scale spill behavior from `model.context_window` alone.
2. Soft spill: write full payload to disk for recoverability, but keep a large inline body when headroom allows (no forced round-trip for “just a bit over 8k”).
3. **One** read bound: a context-derived char budget enforced during line selection, for every `read_file` call — not a line cap, not a post-hoc chop, and not a spill-only carve-out.
4. **Zero user-facing spill knobs** in YAML (same philosophy as fixed context-pressure ratios).

Non-goals:

- Unbounded dumps into history every turn (budgeter + summarization still protect the window).
- Exposing `spill_fraction` / hysteresis / per-tool thresholds to users.
- Making `tools.read_default_lines` window-derived. The 2000-line default stays as the primary "how much comes back" governor; the char budget is a truthfulness-and-safety bound, not the sizing lever.

### Core invariants

> **1. No tool result is ever truncated in history without its full payload on
> disk.** Unconditional — there is no disable hatch that suspends it
> ([No disable hatch](#no-disable-hatch)).
>
> **2. Reported line metadata is always true.** If the payload says `end_line: N`,
> every line up to N is present in the content.

Every rule below exists to keep those true. They are the most important things to
test, and the first draft of this design violated invariant 1 in two places
([Threshold vs. flat cap](#threshold-vs-flat-cap)) while leaving invariant 2
broken for ordinary reads ([`read_file` budgets](#read_file-budgets-all-paths)).

---

## Configuration surface

**Rule: every sizing knob in this design is either derived in code or read from
`monkeybot.yaml`. None is readable from the environment.** The one unresolved case
is `model.context_window`, which is this design's *input* rather than one of its
knobs — see [below](#model_context_window-is-a-real-conflict).

Env vars are an undocumented back channel — they bypass `monkeybot validate`,
never appear in the example yaml, and let a stale `.env` silently override a
deliberate config. The codebase already has the idiom for config-file-only
settings (`auto_schema_enabled_from_config`,
`vertex_google_search_enabled_from_config`: *"Only read from monkeybot.yaml — not
from environment variables"*). Sizing policy follows it.

**Remove** from `tools` YAML / `runtime_env` / example yaml / config docs:

- `spill_min_chars`
- `spill_read_max_lines`

**Do not add** new spill dials (`spill_fraction`, `spill_max_inline_chars`, etc.).

Users only set what they already set for the model:

```yaml
model:
  context_window: 200000  # 128000 / 1000000 / etc.
```

Spill and read policy live in harness code, derived from the window.

### Disposition of every knob in scope

| Knob | Today | After |
|------|-------|-------|
| `tools.spill_min_chars` / `MONKEYBOT_SPILL_MIN_CHARS` | YAML + env | **Deleted.** Derived from window |
| `tools.spill_read_max_lines` / `MONKEYBOT_SPILL_READ_MAX_LINES` | YAML + env | **Deleted.** Derived from window |
| `MONKEYBOT_TOOL_RESULT_MAX_CHARS` | env only, no YAML key | **Deleted.** Derived: floors at `spill_threshold`, and at the read budget for `read_file` |
| `MONKEYBOT_SUMMARY_TOOL_RESULT_MAX_CHARS` | env only, no YAML key | **Deleted.** Derived from window on the same base fraction — thread the *caller's* `window_tokens` from `_summarize_history` through `_summary_line_for_message` → `_flatten_tool_result_for_summary` → `summarize_tool_result_text`. Deriving from a hardcoded window would make "derived from window" a lie; keep a fixed fallback only for callers with no window in hand |
| `MONKEYBOT_TOOL_RESULT_JSON_FIELD_MAX` | env only, no YAML key | **Deleted.** Fixed constant (512); it bounds denylisted blob fields, not context |
| `tools.read_max_lines` / `MONKEYBOT_READ_MAX_LINES` | YAML + env | **YAML only.** Keep the key, drop the env read |
| `tools.read_default_lines` / `MONKEYBOT_READ_DEFAULT_LINES` | YAML + env | **YAML only.** Keep the key, drop the env read |
| `model.context_window` / `MODEL_CONTEXT_WINDOW` | YAML + env | **Unresolved — see below** |

Adjacent but out of scope (not sizing policy; leave alone unless you want the
full sweep): `MONKEYBOT_TOOL_RESULT_SANITIZE` (behavior toggle),
`log_head_lines_from_env` / `log_tail_lines_from_env` in `tool_shapers.py`.

Mechanically this means `workspace_settings_from_env` becomes
`workspace_settings_from_config` (reading the YAML doc, `lru_cache`d — it is on a
per-tool-call path), and the `_int_env` helpers in `core_tool_executor.py` /
`tool_result_ingress.py` lose their spill- and size-related callers.

### No disable hatch

An earlier draft kept `MONKEYBOT_SPILL_MIN_CHARS=0` as an env-only kill switch.
That is exactly what this rule forbids, and `tools.spill: false` in YAML is a
user-facing spill knob by another name. So: **no runtime disable at all.**

Tests that need spill off should inject budgets — pass a `SpillBudgets` into
`CoreToolExecutor` or monkeypatch `spill_budgets_from_window` — which is better
test hygiene than shipping a production env var whose only consumer is the suite.
This also removes the awkward carve-out where invariant 1 did not hold.

### `MODEL_CONTEXT_WINDOW` is a real conflict

This one cannot be quietly converted, and the plan should not pretend otherwise.
It is the **input** to every budget here, but:

- `.github/workflows/live-eval-smoke.yml` sets `MODEL_CONTEXT_WINDOW: "8000"` as a workflow env var.
- `evals/scenarios/context/summarization_trigger.yaml` instructs the operator to set the same env var to force summarization.
- It is read in five places (`bootstrap.py`, `subagents/subagent_worker.py`, `gateway/sse/app.py`, `gateway/sse/routes.py`, `gateway/realtime/routes.py`), four of which are gateway/UI meters unrelated to spill.

Making it YAML-only breaks CI and the eval harness and touches a pre-existing,
widely-read model setting — a separate refactor, not a spill change. Recommended:
**keep the env transport for `model.context_window` in this pass**, and note that
the new sizing policy reads it via `ctx.context_window_tokens` rather than the env
directly, so converting it later is a one-place change. If you want it converted
now, CI and the eval scenario need a config-file mechanism first.

### Retired keys must warn, not vanish

There is no unknown-key rejection anywhere in the config path: `_flatten_config`
only walks `ENV_MAP`, and `validate_monkeybot_yaml_doc` ignores unrecognized
`tools` keys. Deleting the mappings therefore leaves existing configs loading
cleanly while their knob silently stops working.

Add a retired-key list to `runtime_env.py` and log one warning per retired key
found (`tools.spill_min_chars`, `tools.spill_read_max_lines`), surfaced by
`monkeybot validate` as well.

---

## Hardcoded policy (from window)

Use the same cheap estimate as the budgeter: **~4 chars/token**.

One base fraction with fixed multipliers, so the three budgets cannot drift out
of order. Floors are themselves capped by a fraction of the window — otherwise
small windows invert the intent (see the note below).

```python
CHARS_PER_TOKEN      = 4      # matches context_budget._CHARS_PER_TOKEN
SPILL_BASE_FRACTION  = 0.02

INLINE_MULTIPLIER    = 2.5    # spill_threshold * 2.5
READ_MULTIPLIER      = 5.0    # spill_threshold * 5

window_chars = window_tokens * CHARS_PER_TOKEN
base         = window_chars * SPILL_BASE_FRACTION

spill_threshold   = clamp(base,                    min( 8_000, window_chars * 0.05),  64_000)
inline_budget     = clamp(base * INLINE_MULTIPLIER, min(16_000, window_chars * 0.15), 128_000)
spill_read_budget = clamp(base * READ_MULTIPLIER,   min(32_000, window_chars * 0.25), 256_000)
```

Assert `spill_threshold < inline_budget < spill_read_budget` in the helper — the
clamps make this hold at every window, and a future constant edit should fail
loudly rather than silently invert.

### Rough feel

| `context_window` | Spill file at | Max inline when spilled | Spill `read_file` return |
|------------------|---------------|-------------------------|--------------------------|
| 8k (CI smoke) | ~1.6k chars | ~4.8k | ~8k |
| 128k | ~10k chars | ~26k | ~51k |
| 200k | ~16k | ~40k | ~80k |
| 1M | ~64k (ceiling) | ~128k (ceiling) | ~256k (ceiling) |

**Why the floors are fraction-capped:** `.github/workflows/live-eval-smoke.yml`
runs `MODEL_CONTEXT_WINDOW: "8000"` → `window_chars` = 32k. With flat floors, the
8k threshold would be 25% of the entire window and the 16k inline budget 50% of
it — one tool result could eat half the context. The `min(floor, window_chars * f)`
form keeps small windows proportional.

Constants (fractions + clamps) are fixed in code — same as `PRESSURE_*_RATIO` /
`RESULT_BUDGET_FRACTION`.

### Window source

Derive from **`ctx.context_window_tokens`** (already on `TurnContext`,
`core/context/__init__.py`), not a fresh `MODEL_CONTEXT_WINDOW` read in
`CoreToolExecutor.__init__`:

- `execute(self, *, call, ctx)` already has `ctx` in the same method that decides spill.
- `bootstrap.py` and `subagent_worker.py` already resolve the env into that field, so subagents inherit the right value with no extra code.
- A second env parse in the executor is a second source of truth that can diverge.

Wrap the helper in `functools.lru_cache` keyed on `window_tokens` — it runs on
every tool call.

### Threshold vs. flat cap

`spill_threshold` may exceed `MONKEYBOT_TOOL_RESULT_MAX_CHARS` (32,768) at large
windows: at 1M the threshold is 64k, so a 40k-char result is *below* the spill
threshold, gets hard-truncated at 32k by `cap_tool_result_text`, and **has no
spill file to recover from**. `docs/features.md` records the shipped example yaml
at 1M, so this is the default path, not a corner case.

Fix — the sub-threshold branch uses a floored cap:

```python
inline_hard_cap = max(tool_result_max_chars_from_env(), spill_threshold)
```

A sub-threshold result is by definition smaller than `spill_threshold ≤
inline_hard_cap`, so it is never truncated at all. The flat cap keeps its
meaning only for `_SPILL_SKIP_TOOLS` results and errors.

---

## Behavior

### Soft spill (tool result ingress)

When `len(result_text) >= spill_threshold` and tool is not in `_SPILL_SKIP_TOOLS`:

1. **Always** write the **raw** full payload to  
   `.monkeybot/spill/{thread_id}/{call_id}.txt`  
   (unchanged path layout; subagents still use `subagent:{session_id}:{suffix}`).
2. **Inline** up to `inline_budget` chars of the body, then the inventory note last.
   The note must be last — `ContextBudgeter._split_inventory_note` locates it with
   `rfind(_INVENTORY_MARKER)` and treats everything before it as trimmable body.
3. Sanitize the in-history text after the spill write (same order as today: raw on disk, cleaned in history).

**Drop the preview when a body is inlined.** Today's note carries up to
`_PREVIEW_MAX_CHARS` (2000) of shaped preview, which would duplicate the head of
the inlined body verbatim — paying twice and showing the model the same text
twice. Split the note builder:

| Case | In-history text |
|------|-----------------|
| Body fits inline budget whole | body + header (`total chars`, `total lines`, `kind`, `tool`) + pointer line |
| Body is over budget, `kind != json` | truncated body prefix + header + pointer line |
| Body is over budget and `kind == json` | **shaped** JSON that fits the budget (array caps tried widest-first, each keeping the `… (+N more items)` markers) + header + pointer noting the capping |
| Body budget is 0 (pressure, or inline budget smaller than one useful chunk) | header + full deterministic preview + pointer — today's behavior |

**Never inline a raw prefix of JSON.** A prefix of a JSON document is
syntactically broken, and handing the model unparseable JSON is worse than
handing it less data. Shape instead: cap arrays progressively until the
serialized result fits, and fall back to the preview-carrying note when even the
tightest cap will not fit. Classification for the inline case must not build the
preview — the shapers re-serialize the whole payload, which is wasted work when a
body is being inlined.

Suggested shape: `spill_inline_and_note(text, rel_path, *, tool_name, inline_budget) -> tuple[str, str]`
returning `(body_prefix, note)`; keep `spill_inventory_note` for the
preview-carrying case so nothing else changes.

When under threshold: no spill file; sanitize, then cap at `inline_hard_cap`
(budgeter still shapes later).

There is no “never spill” mode — see [No disable hatch](#no-disable-hatch).

#### Accepted exposure: parallel chunks

`inline_budget` is **per result**, and `ToolExecutorPort.execute` takes one call
at a time, so N parallel large results can inline N × 5% of the window in one
history row. Accepted for v1 because:

- Parallel chunks only form for all-`task` calls or all-safe-name tools (`tool_dispatch.py`), which bounds the practical blast radius.
- `ContextBudgeter.fit_content_blocks` divides its pool by `len(blocks)`, so the second line of defense already shrinks per-item budgets as the chunk grows.

If this shows up in practice, the fix is to pass the chunk size into the executor
and divide — not a new knob.

### `read_file` budgets (all paths)

**Decision: unify.** `read_file` is bounded by one mechanism — a window-derived
char budget enforced *during* line selection — for spill paths and ordinary
source files alike. This replaces the earlier draft's spill-only carve-out.

Why unify rather than special-case spill paths:

- The post-hoc 32k chop breaks invariant 2 for **every** large read, not just spill reads (see [the paging bug](#the-read_file-paging-bug-pre-existing)). One char-bounded selection path fixes it everywhere instead of only where spill happens to be involved.
- It deletes strictly more than it adds: the `_is_under_spill_path` line-cap branch, `WORKSPACE_SPILL_READ_MAX_LINES`, the flat-cap bypass plumbing, and "spill reads are special" as a concept in docs and tests. Spill paths end up differing in exactly one field (the default `limit`).
- The greedy-read risk is largely theoretical. The tool schema (`core/context/__init__.py`, `read_schema`) documents `limit` only as "Max lines to return (optional)" — no default, no ceiling, no cost signal — so the model has no reason to volunteer a large `limit`, and the 2000-line default governs in practice.

Four changes, all required together — doing only the first is a no-op:

1. **Char-bounded line selection.** Add `max_chars` to
   `WorkspaceFileService.read_file`. Select lines from `offset` while
   accumulating the **numbered** line length (`width + 1 + len(line) + 1`), and
   stop before exceeding the budget. Never char-chop a selected slice after the
   fact: `end_line` / `truncated` are the paging contract, and a mid-slice chop
   makes `end_line` a lie (invariant 2).
   - Always include at least one line so paging always makes progress.
   - If that single line alone exceeds the budget, hard-slice it, append a `…[line cut at char budget]` marker so the omission is visible, and still report it as the last line — otherwise a single enormous line deadlocks paging forever. The cut remainder is **not** reachable via `offset`, so this case sets `truncated` without promising a `next_offset` that could recover it.
   - Add `next_offset` to `ReadFileResult` so the model does not have to do arithmetic. Emit it **only when whole lines remain** (`end_line < total_lines`), never merely because the payload was cut: an offset past EOF is a dead link. Additive field; existing consumers unaffected.
   - `truncated` is `end_line < total_lines` **or** a line was hard-sliced. A read that reaches EOF within budget reports `truncated: false` and omits `next_offset` — invariant 2 applies to the common case, not just the interesting one.
2. **The flat cap can no longer bind on `read_file`.** Floor
   `cap_tool_result_text` for `read_file` at the read budget, so a truthful
   char-bounded payload is never chopped again on the way into history. With
   selection already bounded, the cap is redundant for this tool.
3. **The default `limit` differs only for spill paths.** Ordinary reads keep the
   2000-line default as the primary governor of how much comes back. Spill-path
   reads drop the line default entirely (chars-only) — paging a large payload back
   is the entire point of a spill read.
4. **An over-budget explicit `limit` clamps, it does not raise.** Today
   `limit > max_lines` raises `invalid_limit`. The char budget is now the
   authority: honor `limit` as an upper bound, clamp to what fits, and report
   `truncated` / `next_offset`.

Also:

- Budget the **content before `_j()`**, at `read_budget * 0.9` — JSON escaping inflates the envelope and the budget's job is to bound what lands in history. Assert the encoded payload stays under the budget in tests.
- Use `spill_read_budget` as the read budget for both cases; the ordinary-read line default keeps typical calls well under it.
- Under turn pressure, `ContextBudgeter` may still trim further (inventory-aware trim already exists).

#### Tell the model the mechanism exists

A budget the model cannot see is a budget it cannot reason about. Extend
`read_schema` so paging is discoverable — one clause each:

- `offset` — "1-based start line (optional). Continue a truncated read from the payload's `next_offset`."
- `limit` — "Max lines to return (optional). Large reads are additionally bounded by a context-derived char budget; check `truncated` / `next_offset`."

#### Accepted cost

A typical large read at a 200k window goes from 32k chars to the full 2000 lines
(~80k), so those calls get roughly 2.5× more expensive in tokens. That is the
same complaint that motivated this design — but it is a real behavior change on
the harness's most-used tool, and it should land in the CHANGELOG as such rather
than as a spill footnote.

### Lifetime / cleanup (unchanged)

- Spills survive across turns within a session.
- Session end cleans parent + `subagent:{session_id}:*` dirs concurrently.
- Do not delete spill at turn start.

### Skip tools (unchanged)

`read_file` and `load_file` do not spill their own results (avoids spill-of-spill).
After unification, spill **paths** differ from ordinary paths in exactly one
respect: no default line `limit`.

---

## Interaction with `ContextBudgeter`

Soft spill uses a **window-derived** inline budget at tool-exec time (it does not
know exact `used_tokens` yet). The budgeter remains the second line of defense —
but as written today it is not a *defense*, it is an unconditional shaper, and it
will eat the inline body this design just granted.

### The problem

`ContextBudgeter._shape_tool_response` applies `shape_tool_text` whenever a
per-tool budget exists, **independent of pressure tier**:

```python
budget = resolve_tool_budget(block.tool_name)
if budget is not None or self.pressure_tier in ("moderate", "aggressive"):
    text = shape_tool_text(...)
```

`tool_output_policy._BUILTIN_TOOL_BUDGETS` gives `run_command`
`max_output_lines=400`, and every registered MCP tool gets
`_MCP_DEFAULT_TOOL_BUDGET` (120 lines). Those are the two largest sources of
oversized output. So a 40k-char soft-spilled `run_command` result is shaped down
to 400 lines at 5% context usage and the inline budget never materializes.

Worse, `shape_logs` has a head-biased path: when enough `keep_patterns` match, it
takes `ordered[:cap]` and **drops the tail** — which is where the spill pointer
lives. The model would get a truncated body with no path to the full payload.

### The fix

In `_shape_tool_response`, treat a block carrying `_INVENTORY_MARKER` as already
budgeted:

1. Split off the note (`_split_inventory_note`).
2. Shape only the body, and only when `pressure_tier in ("moderate", "aggressive")` — skip policy-only shaping, because ingress already sized this against the window and the full payload is on disk.
3. Re-append the note verbatim so the pointer cannot be dropped.

Non-spilled results keep exactly today's behavior.

`_trim_text_to_token_budget` already preserves the note preferentially, so
nothing changes there.

No need to thread live `used_tokens` into `CoreToolExecutor` for v1; window-based
clamps are enough. Because the budgets come from `ctx`, threading remaining
headroom later is a one-argument change.

---

## Implementation sketch

1. ✅ Add a policy helper (`spill_inventory.py` or `context_budget.py`):

   ```python
   @dataclass(frozen=True)
   class SpillBudgets:
       spill_threshold: int
       inline_budget: int
       spill_read_budget: int
       inline_hard_cap: int   # max(TOOL_RESULT_MAX_CHARS, spill_threshold)

   @lru_cache(maxsize=8)
   def spill_budgets_from_window(window_tokens: int) -> SpillBudgets: ...
   ```

   Assert the ordering invariant inside.

2. ✅ `spill_inventory.py`: add `spill_inline_and_note(...) -> (body_prefix, note)`
   with a preview-free note when a body is inlined; keep `spill_inventory_note`
   for the body-less case. **Delete `spill_min_chars_from_env` entirely** — no
   env read survives.

3. ✅ `CoreToolExecutor.execute`: derive budgets from `ctx.context_window_tokens`;
   drop `self._spill_min_chars` / `self._spill_read_max_lines`; soft-spill at or
   above the threshold; cap the sub-threshold branch at `inline_hard_cap`; floor
   the cap at the read budget for `read_file`. No `_is_under_spill_path` branch in
   `_tool_read_file` for line caps, and no per-call bypass flag — unification
   removes both.

4. ✅ `workspace_service.read_file`: add `max_chars`, char-bounded line selection,
   `next_offset` in `ReadFileResult`, clamp-instead-of-raise for over-budget
   `limit`. Spill paths pass `limit=None` meaning chars-only. Retire
   `WORKSPACE_SPILL_READ_MAX_LINES` from `WorkspaceSettings` /
   `workspace_settings_from_env`. Leave the `WorkspaceSettings` line-limit
   *dataclass defaults* alone: the agent path always passes YAML-derived settings,
   so those defaults only reach non-agent callers (the gateway file-viewer
   endpoints), which no model window constrains. Shrinking them would silently
   change a public HTTP endpoint.

5. ✅ `core/context/__init__.py`: extend `read_schema` `offset` / `limit`
   descriptions so `next_offset` paging is discoverable.

6. ✅ `context_budget._shape_tool_response`: inventory-aware shaping (split, shape
   body under pressure only, re-append note).

7. ✅ `runtime_env.py`: remove **four** `ENV_MAP` entries — `spill_min_chars`,
   `spill_read_max_lines` (knobs deleted) and `read_max_lines`,
   `read_default_lines` (knobs kept, env transport dropped). Add the retired-key
   warning list for the first two only; the latter two stay valid YAML.

8. ✅ `tool_result_ingress.py`: delete `tool_result_max_chars_from_env`,
   `summary_tool_result_max_chars_from_env`, and
   `json_field_max_chars_from_env`'s env read. `cap_tool_result_text` takes its
   limit from the caller (derived) rather than resolving it internally.

9. ✅ `workspace_service.py` / `core_tool_executor.py`: replace
   `workspace_settings_from_env` with `workspace_settings_from_config`, reading
   `tools.read_max_lines` / `tools.read_default_lines` from the YAML doc,
   `lru_cache`d because it is on a per-tool-call path.

10. ✅ Tests — the ones that would have caught the bugs above:
   - Threshold / inline / read budgets scale with window; ordering invariant holds at 8k, 128k, 200k, 1M.
   - **8k window:** no single result exceeds ~15% of `window_chars` inline.
   - Soft spill keeps body + note; slightly-over-threshold does not force an empty body; note carries no duplicate preview when a body is inlined.
   - **1M window, 40k-char result:** either untruncated in history or present on disk — never truncated without a file (invariant 1).
   - **`run_command` soft spill at low pressure survives the budgeter** (today's code shapes it to 400 lines) and the pointer line is intact after shaping.
   - **Ordinary (non-spill) read of an 80k-char file at a 200k window:** content matches the reported `end_line` exactly — no post-hoc chop (invariant 2). This is the regression test for the pre-existing paging bug.
   - Spill-path read with no `limit` returns far more than 2000 lines / 32k chars when the window allows.
   - Paging from `next_offset` reaches EOF with no gaps or repeats, on both a spill file and an ordinary source file.
   - Single line longer than the budget still advances `end_line` (no paging deadlock), carries the cut marker, and omits `next_offset` when it is the last line.
   - **A read that reaches EOF reports `truncated: false` and no `next_offset`**, with and without an explicit `limit`. Cheap to get wrong in the selection loop, and it teaches the model to page forever.
   - **JSON over the inline budget stays parseable** — `json.loads` the inlined body, and assert the `… (+N more items)` markers and the array-capped pointer wording are present.
   - Summary cap tracks the caller's window: 8k and 1M windows produce different caps.
   - Encoded `read_file` payload stays under the read budget.
   - Cleanup / subagent paths unchanged.
   - **No env var changes sizing behavior.** Set every retired var
     (`MONKEYBOT_SPILL_MIN_CHARS`, `MONKEYBOT_SPILL_READ_MAX_LINES`,
     `MONKEYBOT_TOOL_RESULT_MAX_CHARS`, `MONKEYBOT_READ_MAX_LINES`,
     `MONKEYBOT_READ_DEFAULT_LINES`) to absurd values and assert results are
     identical. This is the regression guard for the whole rule — env reads are
     easy to reintroduce by habit.
   - `tools.read_max_lines` / `read_default_lines` still take effect from YAML with those env vars set.
   - `test_runtime_env.py` no longer asserts the retired env vars are populated.

---

## Docs / changelog touchpoints

- ✅ `cli/skills/monkeybot/references/config-sections.md` — remove spill knobs; note spill is window-derived; mark `read_max_lines` / `read_default_lines` as YAML-only (no env override).
- ✅ `cli/.../monkeybot.example.yaml` — delete commented spill lines.
- ✅ `docs/features.md` — update the spill invariant to soft spill + window-derived budgets; state both core invariants; record that sizing policy is YAML-or-derived with no env path.
- ✅ `CHANGELOG.md` — two entries, not one. **Spill:** YAML knobs retired (warned, not rejected); behavior scales with `model.context_window`. **`read_file`:** char-budgeted reads replace the flat 32k chop, `next_offset` added, and large reads now return more content than before (behavior change on the most-used tool — see [Accepted cost](#accepted-cost)).

---

## Resolved review questions

1. **Fractions / clamps** — keep 2% with 2.5× / 5× multipliers. Do not go more
   aggressive on 1M: the 128k inline ceiling is already ~32k tokens for a single
   result. The real exposure is per-chunk, not per-result (see
   [Accepted exposure](#accepted-exposure-parallel-chunks)).
2. **Disable hatch** — none. Env-only knobs are forbidden by the configuration
   rule and `tools.spill: false` is a spill knob by another name; tests inject
   budgets instead ([No disable hatch](#no-disable-hatch)).
3. **`TOOL_RESULT_MAX_CHARS` globally** — cannot be left fully as-is. It floors at
   `spill_threshold` for spillable tools ([Threshold vs. flat cap](#threshold-vs-flat-cap))
   and at the read budget for `read_file`, which means it stops binding on that
   tool entirely. Unchanged for `load_file` and errors.
4. **Unify the read path, or carve out spill paths?** Unify — see
   [`read_file` budgets](#read_file-budgets-all-paths). The post-hoc chop breaks
   line metadata for every large read, not just spill reads, and unifying deletes
   more code than it adds.
5. **`read_max_lines` / `read_default_lines`** — stay as-is. The 2000-line default
   remains the primary governor for ordinary reads; only spill paths drop it. The
   char budget bounds truthfully rather than replacing the line default.

---

## Summary

| Before | After |
|--------|--------|
| User tunes `spill_min_chars` + `spill_read_max_lines` | No spill YAML; only `model.context_window` (retired keys warn) |
| Sizing knobs settable from env, some with no YAML key at all | Derived in code or YAML-only; no env path, no disable hatch |
| Hard cut at 8k → inventory only | Soft spill: full file + large inline body, preview-free note |
| All reads bound by a 2000-line default + a post-hoc 32k chop | One window-derived char budget enforced during line selection, for every read |
| `end_line` lies whenever the chop fires — model pages past unseen lines | Line metadata always true; `next_offset` makes paging explicit and discoverable |
| Two read code paths (spill vs. ordinary) plus a cap bypass | One path; spill differs only by dropping the default line `limit` |
| Per-tool line policy silently overrides fresh results | Spill-carrying blocks shaped only under real pressure; pointer always survives |
| 32k–64k results truncated with nothing on disk (1M windows) | Cap floors at the spill threshold — nothing truncated without a file |
| Static policy | Hardcoded fractions/clamps from window (like pressure ratios), asserted ordering |
