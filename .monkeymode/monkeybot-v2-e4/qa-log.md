# Q&A Log — monkeybot-v2-e4 (Subagents, Durability & LLM Council)

## Phase 1A

**Q: Should council run after every turn or once per session?**  
A: Once per session via a configurable idle debounce timer. After each `TurnComplete`, the existing per-session timer is cancelled and a new one starts from `council.idle_seconds` (default 300s / 5 min). When the timer expires with no new turns, council fires once for the full session history. One LLM call per session regardless of turn count. On SIGTERM, `_flush_council_on_shutdown()` fires council immediately for all pending sessions so no memory is lost. (ADR-003)

**Q: stdin/stdout vs Unix socket transport for subagent protocol?**  
A: stdin/stdout JSON-lines chosen. Simpler, zero port management, trivially testable with an echo script, language-agnostic. (ADR-001)

**Q: Separate SQLite DB for durable runs or share the existing monkeybot.db?**  
A: Share the existing DB; add a `durable_runs` table. Consistent with existing patterns, single backup target. (ADR-002)

**Q: Where does `run_council()` get invoked — inline post-turn or background task?**  
A: `asyncio.create_task()` background job fired from `_on_turn_complete` callback. Zero latency impact on the main loop; memory is eventually consistent. (ADR-003)

**Q: What is the subagent timeout (N seconds)?**  
A: 300 seconds (5 minutes) default, configurable globally via `subagents.timeout_seconds` and overridable per named subagent in the registry. Parent emits `ErrorEvent(recoverable=True)` on timeout. (ADR-004)

**Q: How does the council avoid creating duplicate memory entries (e.g. two "user-preferences" files)?**
A: Read-Merge-Write pattern. Before calling the LLM, `run_council()` reads all existing managed category files (`user-preferences.md`, `key-facts.md`, `open-questions.md`) and includes their current content in the prompt. The LLM produces a single complete merged output per category. The code then does a full overwrite — safe because the LLM output already contains everything from the old file plus new information. Deduplication is handled by the LLM's reasoning, not by code. Three fixed category files; dated session files are separate and immutable.

**Q: Can users configure specific named subagents (with their own scripts, skills, models)?**  
A: Yes. Added a `subagents.registry` block to `config.yaml`. Each named entry gets its own `script`, `description`, `skills_path`, `model`, and `timeout_seconds`. `SubagentRegistry` loads this at init and appends a markdown table to the system prompt so the main LLM knows what's available. Ad-hoc spawns by raw script path remain supported. New module: `core/subagent_registry.py`. (ADR-005)
