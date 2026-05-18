# MonkeyBot — Baseline Testing Guide

## Cursor Canvas (optional)

The public repo does not ship a baseline canvas file. If you maintain a private
`internal/` tree (gitignored), copy `internal/canvases/monkeybot-baseline-testing.canvas.tsx`
into `~/.cursor/projects/<your-workspace-hash>/canvases/` to open it in Cursor.

---

## Folder Layout

```
testing/
├── BENCH_NOTES.md          # This file — how to run + baseline results
├── bench.py                # The benchmark runner
├── bots/
│   └── devbot/             # DevBot config used by bench --live and interactive runs
│       ├── AGENT.md
│       ├── MEMORY.md
│       └── config.yaml
└── (future: test data, fixtures, recorded runs)

tests/                      # pytest suite
legacy/tests/               # Legacy pytest suite
```

---

## How to Run

All commands run from the `monkeybot/` directory.

All commands run from the `monkeybot/` repo root.

### Offline only — no API key needed (~400 ms)

```bash
uv run python testing/bench.py
```

### Live LLM — Vertex Claude (GCP creds auto-loaded, no exports needed)

```bash
uv run python testing/bench.py --live
```

### Live LLM — Direct Gemini

```bash
GEMINI_API_KEY=... MODEL_PROVIDER=gemini uv run python testing/bench.py --live
```

### Live LLM — Direct Anthropic

```bash
ANTHROPIC_API_KEY=... MODEL_PROVIDER=claude uv run python testing/bench.py --live
```

### With Docker

```bash
docker compose -f docker/docker-compose.yml up -d
uv run python testing/bench.py --docker
```

### Everything (live + docker)

```bash
uv run python testing/bench.py --live --docker
```

### Pytest suite

```bash
uv run pytest tests/ -v
uv run pytest tests/core/ -v
uv run pytest tests/integration/ -v
```

---

## What Each Section Tests

| Section | What it measures | Needs API key |
|---|---|---|
| 1. Cold Start | Python import time, CLI startup via uv | No |
| 2. Harness TTFT | TTFT, turn latency, streaming tok/s — fake provider | No |
| 3. Memory Ops | File write, keyword search, read latency | No **broken** |
| 4. SQLite History | Save/load conversation turns | No |
| 5. Live LLM | Real TTFT + e2e latency, cold + warm turn | Yes |
| 6. Docker / HTTP | /health, /webhook e2e, 3× concurrent | Yes + Docker |

> **§3 is broken:** `bench.py` calls `save_memory` which was removed from `core/memory.py`.
> Pending resolution of the BACKLOG item "save_memory tool review".

---

## Baseline Results

### Offline — May 13 2026, macOS

| Test | Result | Limit | Status |
|---|---|---|---|
| `import monkeybot` | 13 ms | 2000 ms | PASS |
| `monkeybot --help` (uv) | 72 ms | 5000 ms | PASS |
| `import AgentLoop` | 46 ms | 1500 ms | PASS |
| `import GeminiProvider` | 33 ms | 2000 ms | PASS |
| First turn (DB init) | 3.2 ms | 500 ms | PASS |
| **TTFT (fake provider)** | **1.9 ms** | 100 ms | PASS |
| Warm turn avg (×5) | 2.6 ms | 200 ms | PASS |
| Streaming (100 deltas) | 2.6 ms / ~38 812 tok/s | — | — |
| Write 10 memory files | 1.0 ms | 200 ms | PASS |
| search_memory (10 files) | 0.3 ms | 50 ms | PASS |
| Read memory file | ~0 ms | 20 ms | PASS |
| SQLite save 20 msgs | 18 ms | 500 ms | PASS |
| SQLite load 20 msgs | 0.8 ms | 50 ms | PASS |

**13/13 passing**

### Live LLM — May 14 2026, macOS, Vertex Claude

| Provider / Model | TTFT cold | TTFT warm | e2e cold | e2e warm |
|---|---|---|---|---|
| **vertex-claude / claude-sonnet-4-6@default** | **1873 ms** | **1375 ms** | **2134 ms** | **1552 ms** |
| gemini / gemini-2.0-flash | TBD | TBD | TBD | TBD |
| claude / claude-sonnet (direct) | TBD (no key) | TBD | TBD | TBD |

bench.py hard limit: TTFT < 3000 ms per call. All passing.

### Docker / HTTP — Not yet recorded

Run `bench.py --docker` and record results here.

| Test | Result | Limit | Status |
|---|---|---|---|
| /health response | TBD | 500 ms | — |
| /webhook e2e | TBD | 10 000 ms | — |
| 3× concurrent /webhook | TBD | — | — |

---

## Container Cold Start — Not yet measured

`bench.py --docker` only measures HTTP latency once the container is already running.
To time actual container boot:

```bash
time docker compose -f docker/docker-compose.yml up --wait
```

Record the wall time here once a target is agreed. Suggested target: < 30 s on warm image cache.

---

## Cross-Session Memory Persistence — Not yet automated

No pytest covers process-restart → memory reload. Manual runbook until a test is added:

1. Start an interactive session: `uv run monkeybot run --bot-dir ../sandbox/bots/devbot`
2. Ask the agent to remember something specific (e.g. "Remember: the magic word is BANANA")
3. Exit the process (Ctrl+C)
4. Start a new session
5. Ask: "What is the magic word?"
6. Expected: agent recalls BANANA from memory index

Success criteria: memory is accessible across process restarts without re-prompting.

---

## Feature Test Coverage

| Feature | Test File | Status |
|---|---|---|
| Skills discovery & loader | `tests/skills/test_loader.py` | Covered |
| Skills execution (terminal + loader) | `tests/core/test_terminal.py`, `tests/skills/test_loader.py` | Covered |
| Memory search (INDEX.md) | `tests/core/test_memory.py` | Covered |
| Memory organizer run + index update | `tests/core/test_memory_organizer.py` | Covered |
| save_memory tool (write path) | bench.py §3 + BACKLOG | Broken |
| Cross-session memory persistence | — | Gap — no test |
| Gateway SSE sessions | `tests/integration/test_mb_e2e_simple_reply.py` | Covered |
| SQLite history per thread | `tests/core/test_history.py` | Covered |
| Container cold start timing | — | Gap — no test |
| Live LLM TTFT baseline | bench.py §5 (--live) | No numbers yet |

---

## Target Success Criteria (from monkeybot_v2_plan.md)

| Metric | Target |
|---|---|
| Cold start (import) | < 200 ms |
| Cold start (first token) | < 500 ms |
| Hard runtime dependencies | ≤ 6 |
| Agent loop LOC | ≤ 500 in one file |
| Files to add a new skill | 1 |
| Files to add a new provider | 1 |
| Cloud SDKs in core image | 0 |

---

## Available Vertex Claude Models

| Model | Vertex ID | $/M in | $/M out |
|---|---|---|---|
| Claude Opus 4.7 | `claude-opus-4-7` | $15 | $75 |
| **Claude Sonnet 4.5** (default) | `claude-sonnet-4-5@20250929` | $3 | $15 |
| Claude Haiku 4.5 | `claude-haiku-4-5@20251001` | $0.80 | $4 |
| Claude Opus 4.1 | `claude-opus-4-1@20250805` | $15 | $75 |

---

## How to Update This Baseline

1. Run the full suite: `uv run python testing/bench.py --live --docker`
2. Copy the new numbers into the tables above (Offline, Live LLM, Docker/HTTP sections)
3. Update the date in the Offline section header
4. Run `uv run pytest tests/ -v` and note the pass/fail count
5. If you use the optional baseline canvas under `internal/canvases/`, update it to match (or ask Cursor to regenerate it)
6. Commit changes: `git add testing/ && git commit -m "update baseline YYYY-MM-DD"`

Trigger a baseline update when:
- A provider is changed or upgraded
- A new dependency is added (cold start may regress)
- Memory or history implementation changes
- Any bench section starts failing in CI
