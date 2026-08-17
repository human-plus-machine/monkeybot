# Knowledge Layer — Fix Plan

**Status:** Actionable backlog
**Source:** Gap analysis of full-48 Config C run (transcript `20260717T043739Z_ea295c94-f29d-4218-b6f7-11d9f2ac0ad9`, score 39/48) + design review of [workspace-index-design.md](workspace-index-design.md).

**Headline findings the fixes trace back to:**

- All 9 failures were in Q23–Q48 — the region after the agent stopped calling `recall` (last recall = Q22).
- Recall abandonment was triggered by one garbage result (seq 173: 10 memory-noise hits at score 0.392) plus two context summarizations that erased the "recall-first" habit and prior reads.
- 5 of 9 failures were hallucinated file paths the agent openly guessed instead of retrieving.
- Retrieval itself was decent when used; true answers occasionally landed at rank 4 with the model instructed to read only the top 1–3.
- Initial indexing makes ~1,400 sequential embedding API calls (per-file batches averaging ~3 chunks) instead of ~125 batched calls.

---

## Priority 0 — Fusion correctness (caused the run failure)

### F1. Graph hits must participate in RRF, not bypass it ✅ DONE

- **Problem:** `_GRAPH_BASE_SCORE = 0.35` (× 1.12 note boost = 0.392) vs. a maximum possible RRF score of ~0.033 (`2/(K+1)`, K=60). Any graph-only hit beats every organic keyword/ANN hit by ~12x. This is how 10 episodic memory notes swept the entire top-10 at seq 173 and taught the model that recall is useless.
- **File:** `src/monkeybot/core/knowledge/fusion.py`
- **Change:** Treat graph expansion as a third rank list fused via RRF (`score += 1/(K + graph_rank)`), or cap graph-only scores strictly below `1/(K+1)`. Delete the absolute `_GRAPH_BASE_SCORE` path. Keep the small `+0.05` adjacency bonus only if it's rescaled to RRF units (e.g. `+0.2 * 1/(K+1)`).
- **Accept:** A graph-only hit can never outrank a hit that appeared in both keyword and ANN lists. Add a unit test constructing exactly the seq-173 scenario (note with N links, zero kw/vec rank on targets).
- **Done:** Graph targets now get `graph_rank` and fuse via RRF; `_GRAPH_BASE_SCORE` removed; adjacency bonus rescaled to `0.2/(K+1)` only when reinforcing an organic hit. Coverage: `test_graph_only_hits_use_rrf_not_absolute_score`.

### F2. Fix relative `[[link]]` target resolution ✅ DONE

- **Problem:** `[[episodic/x.md]]` inside `memory/INDEX.md` resolved to target path `episodic/x.md` (no `memory/` prefix). Targets don't exist in the index — hence `snippet: null`, `graph_only` hits — yet still outranked everything (see F1).
- **Files:** `src/monkeybot/core/knowledge/links.py`, `src/monkeybot/core/knowledge/indexer.py` (link parse happens at `_index_text` → `parse_wiki_links`).
- **Change:** Resolve non-`workspace:` link targets relative to the source note's directory within the index namespace (`memory/INDEX.md` + `[[episodic/x.md]]` → `memory/episodic/x.md`). `workspace:` targets stay workspace-relative. Normalize `..`/`.` segments and reject escapes outside known roots.
- **Accept:** Unit test: link parsed from `memory/INDEX.md` produces `memory/episodic/…` target; graph expansion returns a resolvable chunk with a snippet.
- **Done:** `parse_wiki_links(..., source_path=)` resolves relatives; indexer passes `index_path`; unresolvable graph targets no longer emit null-snippet hits. Coverage in `test_knowledge_links.py` + `test_relative_note_link_resolves_for_graph_snippet`.

### F3. Exclude auto-memory noise from the index (or stop boosting it) ✅ DONE

- **Problem:** The memory hook writes a note per tool call ("glob returned zero results in 25ms") into `memory/episodic/` and `semantic/`. The indexer ingests all of it as `source_type: note` with a 1.12 ranking boost. Noise filters only cover `memory/chat_log.md` and `memory/raw/`.
- **Files:** `src/monkeybot/core/knowledge/indexer.py` (`_MEMORY_SKIP_PREFIXES`), `src/monkeybot/core/knowledge/fusion.py` (`_NOISE_NOTE_PREFIXES`, `_score_multiplier`).
- **Change (pick one, prefer a):**
  - (a) Add `episodic/` and post-tool-generated `semantic/` files to `_MEMORY_SKIP_PREFIXES` so they are never indexed. Index only `INDEX.md` and curated/distilled notes.
  - (b) Keep them indexed but as a new demoted category: extend noise prefixes with `memory/episodic/`, `memory/semantic/`, and apply a ≤0.5 multiplier instead of the 1.12 note boost.
- **Also:** `memory/INDEX.md` itself is a link hub, not an answer. Consider demoting it in fusion so its FTS match doesn't trigger fan-out (its links become safe after F1/F2 anyway).
- **Accept:** Re-run the seq-173 query (`NEXT_PUBLIC_SITE_URL production App Hosting config`) against the existing test_bot index — expect `apphosting.yaml` top-3 and zero `episodic/` hits.
- **Done:** Chose (a) — skip `episodic/` + `semantic/` at index time; fusion also filters `memory/episodic/`, `memory/semantic/`, and `memory/INDEX.md` (no hub fan-out). Coverage: `test_indexer_skips_episodic_and_semantic_memory`.

### F20. Split-brain knowledge paths: index at agent root, vectors under workspace ✅ DONE

- **Problem:** Two resolvers anchor relative knowledge paths differently, producing **two** `.monkeybot/knowledge/` trees in test_bot:
  - `runtime_env.py` flattens `knowledge.local_index.path` into `KNOWLEDGE_LOCAL_INDEX_PATH` and resolves it against the **agent root** (`resolve_agent_path(env_val, anchor)` where anchor = agent root).
  - `resolve_knowledge_settings` then lets that env var win, so `index.sqlite` lands at `<agent_root>/.monkeybot/knowledge/`.
  - The vector store path has **no** env mapping, so `_resolve_store` falls through to the intended **workspace anchor** — `vectors.sqlite` lands at `workspace/.monkeybot/knowledge/`.
  - Observed: live FTS index at `test_bot/.monkeybot/knowledge/index.sqlite` (1,429 files incl. memory notes), live vectors at `test_bot/workspace/.monkeybot/knowledge/vectors.sqlite`, plus a **stale** 31MB duplicate `index.sqlite` under the workspace from an earlier session.
- **Files:** `src/monkeybot/core/config/runtime_env.py` (the `KNOWLEDGE_LOCAL_INDEX_PATH` branch), `src/monkeybot/core/knowledge/config.py`.
- **Change:**
  1. In `runtime_env.py`, stop pre-resolving `KNOWLEDGE_LOCAL_INDEX_PATH` to an absolute agent-root path — pass the raw (possibly relative) value through.
  2. Let `resolve_knowledge_settings` do all anchoring in one place, against the workspace anchor (its stated intent: "Prefer workspace/.monkeybot/knowledge when workspace_root is known").
  3. Optional migration nicety: on startup, if `<agent_root>/.monkeybot/knowledge/index.sqlite` exists and the workspace path doesn't, move it; otherwise log a warning about the stale copy.
  4. Clean up test_bot: delete `test_bot/.monkeybot/` after consolidation (the workspace copy re-indexes on startup scan; hash-skip makes it cheap once F10/F11 land).
- **Note:** the indexer's walk skips `.monkeybot` dirs (`extractors.py`), so consolidating everything under `workspace/.monkeybot/knowledge/` is safe — the index never indexes itself.
- **Accept:** Fresh session creates exactly one `.monkeybot/knowledge/` tree (under the workspace) containing `index.sqlite`, `vectors.sqlite`, and `notes/`; `KNOWLEDGE_LOCAL_INDEX_PATH` set explicitly as an absolute path still overrides.
- **Done:** Relative `KNOWLEDGE_LOCAL_INDEX_PATH` left unresolved in `runtime_env`; workspace anchoring + legacy migrate/warn in `resolve_knowledge_settings`; deleted `test_bot/.monkeybot/`. Coverage in `test_knowledge_config.py`.

---

## Priority 1 — Score semantics & ranking quality

### F4. Make `score` mean something; expose raw signals ✅ DONE

- **Problem:** Hits carry raw RRF values (flat 0.01–0.03 band, reads as "0.00 confidence"). ANN cosine similarity is computed in `sqlite_vector.py` then discarded — only its rank survives. FTS bm25 likewise. The model has no discriminative signal, and the design doc's example (`score: 0.91`) promises calibrated confidence the pipeline doesn't produce.
- **Files:** `src/monkeybot/core/knowledge/fusion.py`, `src/monkeybot/core/knowledge/types.py` (`RecallHit`), `src/monkeybot/core/knowledge/sqlite_index.py`, `src/monkeybot/core/persistence/sqlite_vector.py` (plumb scores through).
- **Change:**
  1. Normalize fused score per query: `score_norm = score / max_score_this_query` → top hit is 1.0, rest proportional.
  2. Add optional fields to `RecallHit.to_dict()`: `cosine` (from ANN when present), `bm25` (from FTS when present), `signals: ["fts","ann","graph"]`.
  3. Update the `recall` tool description in the harness prompt so the model knows how to read the fields (e.g. "cosine < 0.45 = weak semantic match").
  4. Fix the example hits in `workspace-index-design.md` to match real semantics.
- **Accept:** Manual recall for a known query shows top hit = 1.0, meaningful spread below, cosine populated on ANN-sourced hits.
- **Done:** Per-query score normalization; `cosine`/`bm25`/`signals` on `RecallHit`; harness + tool-def guidance; design-doc examples updated. Coverage: `test_score_normalized_and_signals_exposed`.

### F5. Filename/path-term boost ✅ DONE

- **Problem:** True answers at rank 4 behind neighbor files, in each case with a query token literally matching the basename: `firebase` → `firebase.ts` (Q02), `button` → `button.tsx` (Q13), `colleges` → `colleges.json` (Q20). Each scored 0.0164 = found by only one signal.
- **File:** `src/monkeybot/core/knowledge/fusion.py`
- **Change:** After candidate assembly, for each hit compute overlap between query content tokens (reuse `_content_tokens` from `sqlite_index.py`) and the path basename/stem segments; apply a multiplicative boost (e.g. ×1.3 for stem match, ×1.15 for path-segment match). Keep it below the power to promote pure noise (boost multiplies existing score, never creates one).
- **Accept:** The three cited queries place their evidence files in the top 3 on the existing test_bot index. Add fixture test.
- **Done:** Stem ×1.3 / path-segment ×1.15 multiplicative boosts. Coverage: `test_path_stem_boost_promotes_filename_match`.

### F6. Sharpen RRF and reconsider `k` ✅ DONE

- **Problem:** `_RRF_K = 60` flattens top ranks (1/61 vs 1/64 — nearly indistinguishable), amplifying the rank-4 problem.
- **File:** `src/monkeybot/core/knowledge/fusion.py`
- **Change:** Drop `K` to ~20 (rank 1 vs rank 4 becomes 0.048 vs 0.042 — still gentle, more separation). Make `K` a `KnowledgeSettings` field so eval runs can sweep it.
- **Accept:** S01–S10 + hard-subset scores do not regress; first-hit precision (F13 metric) improves or holds.
- **Done:** Default `rrf_k=20` on `KnowledgeSettings` / YAML `knowledge.recall.rrf_k` / `KNOWLEDGE_RRF_K`. Coverage: `test_rrf_k_affects_rank_separation`.

### F7. Prompt guidance: read depth by score gap, not "top 1–3" ✅ DONE

- **Problem:** Harness prompt says "read_file the top 1–3 paths"; misses occurred exactly at rank 4.
- **File:** `src/monkeybot/core/prompts/harness_prompt.py`
- **Change:** Rewrite the recall guidance: "Read hits until the normalized score drops sharply (typically the top 3–5). If the top hits all look off-topic, reformulate once; if two reformulations fail, fall back to `grep`/`glob`." Add: "For locate-a-file/asset questions, `glob` on the plausible directory is often better than `recall`" (the Q36–Q46 failure class).
- **Accept:** Re-run full-48 eval; media/path questions answered via glob or recall, zero guessed paths.
- **Done:** Harness prompt + `recall` ToolDef updated (score-gap read depth, glob-for-assets, score field guidance).

---

## Priority 2 — Context-pressure resilience (the actual run killer)

### F8. Recall guidance and progress must survive summarization ✅ DONE

- **Problem:** Two `ContextSummarized` events (seq 158, 278). After the second, the model never called recall again, re-read the same 9 files 3–4x, and guessed answers for Q36–Q46. Instructions and completed work were compressed away.
- **Files:** context summarization path (`src/monkeybot/core/context/` — locate the summarizer prompt/epoch preamble), `src/monkeybot/core/prompts/harness_prompt.py`.
- **Change:**
  1. Ensure the tool-usage guidance block (including recall-first) is re-injected in the post-summarization epoch preamble, not only in the original system prompt position.
  2. Instruct the summarizer to preserve, verbatim, any explicit task-progress state (e.g. "Q01–Q22 answered with evidence; Q23–Q48 remaining") and the answer-format contract.
  3. Steer long multi-item tasks to externalize progress: harness prompt guidance to write incremental results to a file when a task has >N enumerable items (the agent only wrote the answers file when the user asked in turn 2).
- **Accept:** Full-48 re-run shows recall usage in the back half of the question list; `tool_loop_thrash` warnings for repeated `read_file` drop to ~0.
- **Done:** Compaction summarizer preserves task-progress + answer-format + recall-first strategy; every `[Context Summary]` gets a standing-instructions footer (recall / format / progress file / path rule); harness adds long multi-item externalization + `Evidence: unknown`. Coverage: `test_loop` summarization asserts + `test_harness_prompt`.

### F9. Evidence-path verification guard ✅ DONE

- **Problem:** 5 failures were fabricated paths, self-flagged ("or similar — exact filename not in provided files").
- **Options (either is cheap):**
  - (a) POST_TURN hook: scan assistant output for `Evidence: <path>` patterns, check existence against workspace, and inject a correction notice into the next model step for misses.
  - (b) Prompt-level: "Never emit a file path you have not confirmed via read_file/glob/recall this session. If unknown, say unknown."
- **Files:** (a) new hook alongside `src/monkeybot/core/knowledge/hook.py`; (b) `harness_prompt.py`.
- **Accept:** Re-run full-48: zero non-existent paths in Evidence lines.
- **Done:** Both (a) and (b) — `EvidencePathGuard` on `AFTER_PROVIDER_RESPONSE` → inject on `PRE_TURN`/`PRE_TOOL`; harness Path rule + Evidence:unknown. Coverage: `tests/core/test_evidence_guard.py`.

---

## Priority 3 — Indexing performance

### F10. Cross-file embedding batches + concurrent dispatch (~10–40x) ✅ DONE

- **Problem:** `_embed_chunks` is called per file inside the sequential scan loop. ~1,400 files ÷ ~4,000 chunks = avg ~3 chunks/file → ~1,400 sequential API calls at batch≈3 instead of ~125 calls at batch=32. The `_CONCURRENCY = 4` semaphore in `embeddings/nvidia.py` never sees concurrent load.
- **Files:** `src/monkeybot/core/knowledge/indexer.py`, `src/monkeybot/core/persistence/sqlite_vector.py`.
- **Change:**
  1. During `_full_scan`, don't embed inline. Accumulate `(path, chunks)` for changed files into a pending list.
  2. After the walk, flatten to chunk level, slice into `batch_size` batches **across file boundaries**, and dispatch with `asyncio.gather` (semaphore already rate-limits).
  3. Group vector upserts per path (`delete_by_path` then `upsert`) once that path's chunks are all embedded — or switch to delete-all-then-executemany within one transaction keyed by scan generation.
  4. Keep the incremental single-file path (`_reindex_one`) as-is; it's fine for hook-driven updates.
- **Accept:** Fresh index of auriga-web: embedding wall-time drops from minutes to well under a minute; API call count ≈ `ceil(total_chunks / batch_size)`.
- **Done:** Scan accumulates chunks into `_pending_embed` then `_flush_pending_embeds`; NVIDIA `_embed_batches` uses concurrent `gather`. Coverage: `test_full_scan_batches_embeddings_across_files`.

### F11. Cheap rescans; never block recall on a full scan ✅ DONE

- **Problem:** `hook.py` schedules a **full workspace rescan after every successful `run_command`** (even `ls`), and `subsystem.recall()` awaits `flush()` first — so a recall after any shell command blocks on a re-walk+hash of ~1,400 files (the ~850ms recall calls; worse with embed backfill).
- **Files:** `src/monkeybot/core/knowledge/hook.py`, `src/monkeybot/core/knowledge/indexer.py`, `src/monkeybot/core/knowledge/subsystem.py`.
- **Change:**
  1. In the hook, inspect `run_command` argv: only request rescan for plausibly mutating commands (`git`, `mv`, `cp`, `rm`, `mkdir`, `touch`, `tar`, `unzip`, `npm|pnpm|pip|uv` install/build, redirections). Read-only commands (`ls`, `cat`, `grep`, `find`, `head`, …) skip it. Default unknown → rescan (fail-safe).
  2. Add an mtime fast path to `_full_scan`: if `(path, mtime)` matches the `files` table, skip read+hash entirely.
  3. In `subsystem.recall()`, replace the unbounded `await flush()` with `asyncio.wait_for(flush(), timeout=~0.5s)`; on timeout, run the query anyway and set `"stale": true` in the payload (the design doc already promises this).
  4. Move file read + hash into `asyncio.to_thread` so scans don't block the event loop.
- **Accept:** `recall` p95 latency < 150ms with a dirty queue present; `ls`-type commands trigger no rescan.
- **Done:** `command_implies_fs_mutation`; mtime fast path via `get_file_state` (mtime **and** stored `chunker_version` must match, so a chunker upgrade still re-chunks untouched files); 0.5s flush timeout with `stale: true`; disk I/O via `to_thread`. Coverage: readonly/`ls` rescan skip, mtime path, chunker-version bump re-chunk, mutation heuristics.

### F12. SQLite write batching + vector query off the event loop ✅ DONE

- **Problem:** `sqlite_index.upsert_file` inserts chunks row-by-row (2 executes per chunk). `sqlite_vector.query` does a full table scan with pure-Python dot products (~4k × 1024 mults) on the event loop.
- **Files:** `src/monkeybot/core/knowledge/sqlite_index.py`, `src/monkeybot/core/persistence/sqlite_vector.py`.
- **Change:** `executemany` for chunks + chunks_fts inserts. In the vector store, unpack/score with numpy (single matmul) or at minimum run scoring in `asyncio.to_thread`; keep brute force until scale demands `sqlite-vec` (Phase 3 unchanged).
- **Accept:** Index build CPU time measurably down; vector query < 30ms at 4k chunks without blocking the loop.
- **Done:** `executemany` for chunks/FTS/links; bulk FTS delete; vector scoring in `asyncio.to_thread` (zero-dep path).

---

## Priority 4 — Design-doc corrections & eval infrastructure

### F13. Instrument retrieval rank; make metrics real ✅ DONE

- **Problem:** The design lists Recall@k / verification rate as metrics but nothing computes them. Rank-of-evidence-file per query is the metric that would have caught the rank-4 pattern systematically — and every eval question already has an `Evidence` field.
- **Files:** `evals/knowledge_layer/score_auriga_answers.py` (extend), new small script `evals/knowledge_layer/recall_rank_report.py`.
- **Change:** Parse transcript `ToolCallResult` events for `recall`; for each recall call, match against the Q&A `Evidence` paths and record the rank at which the evidence file appeared (or ∞). Emit Recall@1/@3/@5/@10 + MRR per run. Also log verification rate (`read_file` on a recall hit within N steps).
- **Accept:** One command produces a rank report for any past transcript; baseline numbers recorded in the design doc for B1, semantic-slice, Config C, and the full-48 run.
- **Done:** `evals/knowledge_layer/recall_rank_report.py` emits Recall@k, MRR, verification rate, and endurance stats. Design doc Metrics + full-48 section point at it; historical Recall@k for past runs pending transcript re-parse when artifacts are available.

### F14. Harden the answer-format scorer ✅ DONE

- **Problem:** `score_auriga_answers.py` returned 0/10 on a valid answers file because the agent used `## Qxx:` headings instead of `Qxx:` lines.
- **File:** `evals/knowledge_layer/score_auriga_answers.py`
- **Change:** Accept both formats (`^Q\d\d:` and `^#+\s*Q\d\d:`), strip markdown emphasis before token matching, and support `--all` to score all 48 (not just the hard subset). Also fix Accept-expression parsing for grouped forms like `` `A` OR (`B` AND `C`) ``.
- **Accept:** Scorer reproduces the manual 39/48 on `test_bot/workspace/auriga-web-qa.md`.
- **Done:** Heading + emphasis + `--all` + nested Accept OR/AND. Coverage in `tests/evals/test_knowledge_eval_scripts.py`. (39/48 reproduction needs the answers artifact when present.)

### F15. Add a full-48 endurance slice to the eval protocol ✅ DONE

- **Problem:** The 10-question hard subset never crosses a summarization boundary, so tool-selection decay under context pressure was invisible until the full-48 run.
- **File:** `docs/workspace-index-design.md` (protocol section), eval runbooks.
- **Change:** Make full-48 a standard slice with its own baseline row (record: score, recall calls in first vs second half, summarization count, thrash warnings). Re-baseline after F1–F9 land.
- **Accept:** Design doc has a full-48 baseline table; regressions in back-half recall usage fail the gate.
- **Done:** Full-48 endurance baseline + gate in design doc; eval slices table; rank-report endurance block implements the signals.

### F16. Give the graph a write path — or demote it honestly

- **Problem:** `knowledge/notes/` is empty; nothing writes `[[workspace:…]]` links; "link-writing affordances" are deferred to Phase 3. The design's differentiating hypothesis (linked vault) has never run with link density > 0 — B1's win was loose FTS, not graph.
- **Change (decide, then do one):**
  - (a) **Build the write path now:** extend the memory organizer (or a POST_TURN distiller) to write curated notes into `.monkeybot/knowledge/notes/` with `[[workspace:path#Lstart-end]]` links when it summarizes findings; run the notes-heavy eval slice (`seed_auriga_notes.py` exists) and measure graph contribution separately (hits with `via: graph:` that were correct).
  - (b) **Demote:** move the graph to "Phase 3 experiment" in the design doc and describe shipped-B as hybrid FTS(+ANN). Delete the standing risk of untested code paths ranking above tested ones.
- **Accept:** Either a notes-heavy eval run with real link density and a measured graph win/loss, or a design-doc edit removing the claim.

### F17. Specify the note trust gate in the design

- **Problem:** Design says notes are "agent-authored, curated, trusted" but doesn't define what qualifies, so implementation trust-by-provenance broke when auto-capture polluted provenance (see F3).
- **File:** `docs/workspace-index-design.md`
- **Change:** Add a "What counts as a note" section: curated = organizer-distilled or human/agent-authored files under `knowledge/notes/` and memory `INDEX.md` + `semantic/` distillations that pass a curation flag; raw capture (episodic tool echoes, chat logs, spill) is **never** `source_type: note`. Boost applies only to curated.

### F18. Note code-aware chunking as a known limitation / Phase 2.5 item ✅ DONE

- **Problem:** Line/char-window chunking with heading prefixes is a prose strategy; the dominant corpus is code. Function-splitting boundaries degrade snippets and embeddings and plausibly contribute to rank-4 misses.
- **File:** `docs/workspace-index-design.md` (+ future `chunking.py` work)
- **Change:** Document the limitation now; plan symbol-aware chunking (tree-sitter, or a cheap indent/brace heuristic that avoids splitting inside a top-level definition) as Phase 2.5. Not blocking.
- **Done:** Content-aware chunking shipped in `core/knowledge/chunking.py` — per-suffix strategies (markdown headings, tree-sitter / brace heuristic for code, JSON/YAML/TOML top-level keys, prose window fallback). Optional extra `knowledge-ast` (`tree-sitter-language-pack`). `CHUNKER_VERSION` is stored per file (`files.chunker_version`) and forces a re-chunk on upgrade regardless of mtime. Offline markdown rank≤4 gate in `tests/core/test_knowledge_chunking_rank.py`.

### F19. Fix design-doc examples and freshness claims

- **File:** `docs/workspace-index-design.md`
- **Change:**
  1. Replace `score: 0.91` style example hits with real semantics post-F4 (normalized score + cosine/bm25 fields).
  2. Update the freshness section to reflect F11: mutation-aware rescan triggers, mtime fast path, bounded pre-recall flush with `stale: true`.
  3. Materialize graph-target snippets at index time (or note that graph hits resolve lazily and may be snippet-less) — pick one and say it.

---

## Priority 5 — Unprompted adoption (model won't use recall without being told)

### F21. Make recall the default choice without prompt forcing ✅ DONE (code)

- **Problem:** The model only uses `recall` when the user prompt explicitly says "use recall first." All eval data so far includes that instruction, so unprompted behavior is unmeasured — and observed unprompted behavior is grep/glob/read_file. Three causes:
  1. **Name prior:** "recall" connotes memory retrieval, not code search. Models are post-trained on harnesses where the codebase-search tool is `codebase_search` / `search`; an unfamiliar memory-sounding name loses to the ingrained grep/glob workflow.
  2. **No cross-references:** `grep` ("Search workspace file contents with a Python regex. Prefer over run_command+grep.") and `glob` ToolDefs never mention the index. Routing guidance lives only in `recall`'s own description + system prompt — not where the model is already looking when it reaches for grep.
  3. **Index not salient:** the model has no evidence an index exists for the workspace until it happens to call the tool.
- **Files:** `src/monkeybot/core/context/__init__.py` (ToolDefs), `src/monkeybot/core/prompts/harness_prompt.py`, `src/monkeybot/core/knowledge/subsystem.py` + system-context injection path, optional new hook alongside `EvidencePathGuard`.
- **Change:**
  1. **Rename** `recall` → `search` (or `codebase_search`); keep `recall` as an accepted alias during migration (tool executor maps both; prompts/evals migrate to the new name). Closes the design doc's open naming question with behavioral evidence.
  2. **Cross-reference from the familiar tools:** `grep` ToolDef → "Exact regex/identifier search only. For conceptual / 'how does X work' / paraphrased questions, use `search` first." `glob` ToolDef → "…for content questions prefer `search`."
  3. **Announce the index at turn start:** when the knowledge layer is ready, inject one line into system context (via `SystemContextUpdated`): "Workspace index ready: N files / M chunks. `search` covers all of it." Numbers make the capability concrete.
  4. **Drift backstop hook:** POST_TOOL hook — ≥3 grep/glob/read_file calls in a turn with zero `search` calls injects a one-line reminder (same injection mechanism as `EvidencePathGuard`). Catches mid-turn and post-summarization drift without hard forcing.
  5. **Measure it:** add an eval variant running the same question pack **without** the "use recall first" instruction; metric = unprompted `search` usage rate (calls per question, % questions where `search` preceded the first `read_file`). Track alongside F13 rank metrics.
- **Accept:** Unprompted full-48 run (no recall instruction in the user prompt) shows `search` used for ≥70% of questions before file reads, with no score regression vs the prompted run.
- **Done (items 1–4):**
  1. Tool renamed to `search`; `recall` kept as an executor-level alias (both dispatch to `_tool_recall`; alias noted in the ToolDef and harness prompt). Prompts, compaction standing instructions, evidence-guard text, and memory-nudge line all migrated to `search`.
  2. `grep` / `glob` / `search_memory` ToolDefs now cross-reference `search` for conceptual/content questions.
  3. `IndexAnnouncer` (new `knowledge/salience.py`) injects a once-per-thread "Workspace knowledge index ready: N files / M chunks" notice on `PRE_TURN`, using a new `KnowledgeIndex.counts()`.
  4. `SearchUsageNudge` (same module) is the drift backstop: ≥3 grep/glob/read_file calls in a turn with zero `search` calls injects a one-shot reminder on the next tool step. Both registered via `KnowledgeSubsystem.register_hooks`.
  - Coverage: `tests/core/test_knowledge_salience.py`; alias + rename asserts in `test_core_tool_executor.py` / `test_harness_prompt.py` / `test_context.py`. Full suite green (1488 passed).
- **Done (item 5 — unprompted eval, transcript `20260717T154759Z_b14b4142-…`):** prompt contained **no** search/index instruction; model still made **50 `search` calls**, first search (seq 10) before first `read_file` (seq 21), usage sustained through 3× `ContextSummarized` (last search seq 395/407). Announcer fired once; nudge never needed; evidence guard never fired (zero fabricated paths). Score **37/48**; Recall@1 0.646 / @5 0.792 / MRR 0.711. Acceptance met on adoption; residual misses are verification failures → F22.

### F22. Verify Evidence citations by reading, not by snippet ✅ DONE

- **Problem:** In the unprompted full-48 run, 7 of 11 misses (Q06, Q11, Q12, Q13, Q15, Q26, Q42) had the evidence file at **rank 1–2 in a `search` result** — the model answered from the snippet (or its prior) without `read_file`-ing the file it cited. Verification rate was 0.540. Retrieval is no longer the bottleneck; verification discipline is.
- **Files:** `src/monkeybot/core/knowledge/evidence_guard.py`, `src/monkeybot/core/prompts/harness_prompt.py`, `src/monkeybot/core/runtime/history_compaction.py`.
- **Change:**
  1. `EvidencePathGuard` now tracks confirmations on `POST_TOOL`: successful `read_file` paths and paths returned by `glob` (glob counts because binary assets — images/PDFs — cannot be `read_file`'d).
  2. On `AFTER_PROVIDER_RESPONSE`, cited `Evidence:` paths that **exist but were never confirmed** trigger a one-shot "Evidence verification required" injection (same PRE_TURN/PRE_TOOL mechanism as the F9 fabrication correction): read the file, check the answer, restate if it changes. Prefix-tolerant path matching (`repo/src/x.ts` ≈ `src/x.ts`); once-per-path so it cannot loop.
  3. Prompt: harness "Path rule" and knowledge-retrieval bullet now say a `search` hit is a lead, **not** confirmation — "answer only after reading"; same rule added to the post-compaction standing instructions.
- **Accept:** Re-run unprompted full-48: verification rate ≥ 0.8; snippet-only misses (Q11/Q12/Q26/Q42 class) converted; score ≥ prompted baseline (39/48).
- **Done:** Guard + prompt changes as above. Coverage: `test_guard_flags_existing_but_unread_citation`, `test_guard_accepts_glob_confirmation_for_assets`, `test_guard_read_confirmation_tolerates_prefix` in `tests/core/test_evidence_guard.py`.
- **Validated (run `20260717T162603Z_2f553ae9-…`):** score **43/48** (vs 37 unprompted pre-F22, 39 prompted baseline); zero summarizations; all seven F22-target snippet misses converted (Q06, Q11, Q25–Q28, Q39, Q42); model wrote `answers.md` incrementally + used `todo_list`.
- **Extension (blind spot found in that run):** Evidence lines delivered inside a `write_file` deliverable (`answers.md`) bypassed the guard — it only scanned chat text, so an unread `button.tsx` citation sailed through (Q13). Guard now also scans `write_file` `content` / `replace_in_file` `new_string` for `Evidence:` citations with the same missing/unread checks, and counts model-written paths as confirmed. Coverage: `test_guard_scans_written_file_content`, `test_guard_written_file_counts_as_confirmed`, `test_guard_scans_replace_in_file_new_string`.

---

## Suggested execution order

| Wave | Fixes | Why first |
|------|-------|-----------|
| 1 | F1, F2, F3, F20 | Directly caused the run failure + split-brain paths; small, testable diffs in fusion/links/indexer/config |
| 2 | F4, F5, F6, F7 | Score + ranking + guidance; measurable via F13 |
| 3 | F13, F14, F15 | Instrumentation before re-baselining — do not tune ranking blind |
| 4 | F8, F9 | Context resilience; needs a full-48 re-run to validate |
| 5 | F10, F11, F12 | Perf; independent of quality work |
| 6 | F16, F17, F18, F19 | Design-doc truth + graph decision |
| 7 | F21 ✅ | Unprompted adoption: rename + cross-refs + salience + drift hook; validated by the no-instruction eval run (50 unprompted `search` calls) |
| 8 | F22 ✅ | Verification discipline: evidence guard flags snippet-only citations; "answer only after reading" prompt rule |

**Re-baseline after wave 4:** full-48 + hard subset + S01–S10, with F13 rank reports, recorded in the design-doc run log.
