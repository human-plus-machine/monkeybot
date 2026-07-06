# Review: PR #62, #63, #64, #65 (merged to main by Karthik)

Reviewed against actual code at `origin/main` (0f57041), not just diffs, since #65 is
the `develop`→`main` merge that bundles #62+#63+#64 (nothing extra in #65 itself).

- **#62** `feat(context): limit curation to memory index only` — drop skill selection from curator
- **#63** `fix: list_skills returns SKILL.md frontmatter descriptions`
- **#64** `feat(gemini): Vertex native google_search grounding`
- **#65** merge of the above into `main`

---

## CRITICAL

None.

---

## HIGH

### H1 — #62 shipped a regression that had to be silently hotfixed same-day, off-PR
`curator.py` / `harness_prompt.py` / `prompt.py`

#62 removed skill names from the system prompt entirely (not just from curator
selection). This broke skill discovery for every agent turn. It was patched by commit
`9175fc9` ("fix(context): restore skill names in prompt and add session teardown"),
pushed **directly to `develop`**, same day, with no PR/review — it rode along in the
history between #62 and #64.

**Why it matters:** this is exactly the "ruins prompt construction per turn" failure
mode you're worried about, and it shipped. The fact that it needed a hotfix means #62's
own test suite didn't catch a regression in the most basic prompt content (skill
visibility). Worth asking Karthik whether other silent hotfixes like this happened
outside the 4 PRs you were shown.

### H2 — Gemini grounding + function-calling combination was never actually verified
`src/monkeybot/providers/gemini.py:563-568`, `tests/providers/test_gemini_usage.py`

`stream()` adds `types.Tool(google_search=...)` alongside `types.Tool(function_declarations=decls)`
in the same request whenever `vertex_google_search=True`. But:

- Every real agent turn already carries function-call tools (`read_file`, `write_file`, `run_command`, …).
- The only test for this (`test_stream_adds_google_search_tool_when_opted_in`) calls `stream()` with `tools=[]` — it never exercises the combined-tools path that is the actual production code path.
- The PR's own test plan left both manual-verification checkboxes **unchecked**:
  - `[ ] Manual: ... confirm CLI shows grounded sources`
  - `[ ] Manual: confirm summarization/curator turns do not attach google_search tool`

Vertex/Gemini has historically restricted mixing native tools (search grounding) with
user function declarations depending on model generation. Nobody confirmed this works
against the real API before merge — this is the everyday code path once the flag is on,
not an edge case. **Verify manually against live Vertex before enabling this flag anywhere real.**

---

## MEDIUM

### M1 — New memory-curation subsystem is over-built for what it does
`src/monkeybot/core/context/memory_prompt.py`, `curator.py`, `runtime_env.py`

One feature ("pick which memory lines go in the prompt") now has **9 env
knobs**: `CONTEXT_CURATION_ENABLED`, `_MODE`, `_MEMORY_WINDOW_LINES`,
`MEMORY_INDEX_CAP`, `_MEMORY_THRESHOLD`, `_MEMORY_TOKEN_THRESHOLD`, `_CURATOR_MODEL`,
`_TIMEOUT_SEC`, `_MAX_MEMORY_LINES`, `_SEARCH_MAX_HITS` — plus 3 modes (`window` /
`curator` / `hybrid`) with a 4-way branch matrix in `prepare_memory_for_prompt`.

Nobody asked for tunable curation modes; this reads like config surface added
speculatively. Recommend collapsing to one behavior (window-with-fallback-to-curator-on-token-overflow,
i.e. today's `hybrid` default) and deleting `window`/`curator` as selectable modes plus
their env vars, unless there's a concrete user asking for the other modes.

### M2 — Curator LLM call sits in the blocking hot path of every turn
`src/monkeybot/core/runtime/loop.py:948-970`

When the memory index changes and is token-heavy (default >2000 tokens), the turn loop
`await`s a second, separate LLM streaming call (up to `CONTEXT_CURATION_TIMEOUT_SEC=10s`)
**before** building the system prompt and starting the real model call. It's cached by
index fingerprint so it doesn't refire every turn, but the first turn after any memory
write pays this full latency serially. This is a real per-turn latency risk for exactly
the "don't ruin per-turn construction" concern — worth confirming this is acceptable, or
moving it to a background pre-compute that the turn loop just reads.

### M3 — Default thresholds are internally inconsistent
`memory_prompt.py:69,72` — default `CONTEXT_CURATION_MEMORY_THRESHOLD=8` but default
`CONTEXT_CURATION_MEMORY_WINDOW_LINES=12`. Curation "triggers" past 8 lines, but the
window (12) still shows the *entire* index until it exceeds 12 lines — so in the default
config, curation is a no-op between 9–12 memory lines. Harmless today, but signals the
defaults weren't reasoned through together.

### M4 — Dead, duplicated env-var helper
`memory_prompt.py:72-73` defines `memory_index_cap_from_env()` reading `MEMORY_INDEX_CAP`
— it is **never called anywhere**. Meanwhile `organizer.py:273-278` has its own private
`_index_cap_from_env()` reading the same env var with the same fallback logic,
independently. Delete the dead one in `memory_prompt.py`, or have `organizer.py` import
the shared one — pick one, not both.

### M5 — No test covers the default (`hybrid`) mode's actual branch
`tests/core/test_memory_prompt.py` tests `window` mode, curator-cache-hit, and
curator-failure-fallback — but never exercises the token-heavy branch inside `hybrid`
mode, which is what ships by default (`CONTEXT_CURATION_MODE` defaults to `"hybrid"`).
The one behavior every user actually gets is the one behavior nobody tested.

---

## LOW

### L1 — `vertex_google_search: bool` threaded through ~12 function signatures
`loop.py`, `provider.py`, `gemini.py`, `spans.py`, `instrumentation.py`,
`subagent_worker.py` all grew a `vertex_google_search: bool = False` parameter passed
straight through. Functionally fine (it's a real per-call flag, not fake config), but
it's a lot of plumbing for one boolean — a `RequestOptions`/context-style bag would
avoid re-touching every call site the next time a similar per-call provider flag shows up.
Not urgent; note it if a second flag like this gets added.

### L2 — Duplicated tiny env-parsing helpers
`_env_int` / `_env_float` are defined identically in both `curator.py` and
`memory_prompt.py`. Trivial (~10 lines), but one already imports from the other module —
just import the helper instead of copy-pasting it a second time.

### L3 — `evals/uv.lock` (+2153 lines) — false alarm, not harness bloat
Flagging this so you don't have to chase it: `evals/` is a separate sub-project with
its own `pyproject.toml`/`Dockerfile`, and `docker/Dockerfile` (the actual harness image)
only `COPY`s `src`, `.agents`, `pyproject.toml`, `README.md` — `evals/` is never copied
in. This lockfile regen doesn't touch image size. It *was*, however, bundled into an
unrelated commit (`9175fc9`, the context hotfix) — a scope/hygiene nit on commit
discipline, not a runtime issue.

---

## Good, worth calling out
- #63's actual bug fix (`_parse_skill_description` reading YAML frontmatter) is clean,
  correctly falls back to the first body line for skills without frontmatter, and is
  tested.
- Curator failure paths fail open to the deterministic window slice rather than
  breaking the turn — sensible default-safe behavior.
- New `DELETE /sessions/{id}` correctly evicts the curation cache
  (`session_bus.py:191-209`), so that cache doesn't grow unbounded — the one lifecycle
  concern in this PR set that *was* handled correctly.
- Gemini grounding events are threaded cleanly through the provider → runtime event →
  SSE → CLI pipeline with no loose ends (`GroundingEvent` dataclasses match up end to end).

---

## Suggested order of attack
1. H2 — get one real Vertex call with grounding + full tool set and confirm it doesn't 400.
2. H1 — ask Karthik if there were other off-PR hotfixes; add a regression test for skill-name presence in the prompt so it can't silently disappear again.
3. M1 — decide whether `window`/`curator` modes are staying; if not, delete them and the dead env vars now before anyone builds on top.
4. M4, L2 — quick dead-code/duplication cleanup, 10 minutes each.
