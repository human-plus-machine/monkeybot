# Memory System

monkey-bot provides a multi-layer memory system that gives your agent persistent context across conversations, restarts, and container instances.

---

## Overview

```
┌─────────────────────────────────────────────────────┐
│                 Memory Layers                       │
│                                                     │
│  Layer 0 (Identity Files)                           │
│  ├── SOUL.md          Personality, values           │
│  ├── IDENTITY.md      Role, responsibilities        │
│  ├── USER.md          Known user preferences        │
│  └── INDEX.md         Map of stored memory          │
│                                                     │
│  Layer 1 (Session Memory)                           │
│  ├── GCSStore         Long-term key-value store     │
│  ├── Session Summaries Conversation digests         │
│  └── search_memory    Tool for semantic recall      │
│                                                     │
│  Layer 2 (Conversation State)                       │
│  └── Checkpointer     Per-thread message history    │
│      (InMemory / Firestore)                         │
│                                                     │
│  Layer 3 (Raw Observations)                         │
│  ├── data/memory/raw/  Unprocessed observations     │
│  └── LLM Council       Classifies + indexes raw     │
└─────────────────────────────────────────────────────┘
```

---

## Layer 0: Identity Files

These markdown files are read at startup and injected into the agent's system prompt. They give your agent a consistent identity across all conversations.

**Location:** `{memory_dir}/` (default: `./data/memory/`)

### SOUL.md

Core personality, values, and communication style. Kept short — under 500 tokens.

```markdown
<!-- data/memory/SOUL.md -->
I am direct and precise. I prefer clarity over comprehensiveness.
I use bullet points for lists, not paragraphs.
I ask one clarifying question at a time.
When I don't know something, I say so rather than guessing.
I use technical language with engineers, plain language with everyone else.
```

### IDENTITY.md

Role, responsibilities, and operational context. Under 800 tokens.

```markdown
<!-- data/memory/IDENTITY.md -->
I am the engineering assistant for the Platform team.
I have access to the codebase via the file-ops skill.
My primary contacts are Alice (alice@company.com) and Bob (bob@company.com).
I can run diagnostics, schedule reports, and answer technical questions.
My memory is stored at gs://my-bot-memory/data/memory/.
```

### USER.md

Known preferences and context for the primary user. Updated by the agent over time.

```markdown
<!-- data/memory/USER.md -->
Alice prefers concise responses, 3 bullet points max.
Alice's timezone is America/New_York.
Bob prefers detailed explanations with code examples.
Both users are comfortable with Python and Kubernetes.
```

### INDEX.md

An auto-generated or manually maintained map of what's stored in memory. The LLM Council updates this file after each memory processing cycle.

```markdown
<!-- data/memory/INDEX.md -->
## episodic/
- 2026-03-01-deployment-incident.md: P1 incident on March 1, API gateway downtime
- 2026-02-28-architecture-review.md: Team discussed moving to event-driven architecture

## semantic/
- kubernetes-runbook.md: Steps for common k8s operations
- api-rate-limits.md: Rate limit thresholds for all downstream services

## procedural/
- deploy-checklist.md: Pre-deploy checklist, last updated Feb 2026
```

**Token budget guidance:**
- `SOUL.md` → 500 tokens max
- `IDENTITY.md` → 800 tokens max
- `INDEX.md` → 1000 tokens max (framework warns if exceeded)

---

## Layer 1: Session Memory (GCSStore)

For production deployments, enable GCS-backed long-term memory that persists across container restarts and scales across instances.

### Enabling GCS Memory

In `bot.yaml`:

```yaml
memory:
  backend: gcs
  bucket: my-bot-memory-bucket
```

In `src/main.py`:

```python
from emonk.core.store import GCSStore

store = GCSStore(
    bucket_name="my-bot-memory-bucket",
    project_id="my-gcp-project",
)

agent = build_deep_agent(
    model=model,
    tools=tools,
    user_system_prompt=prompt,
    store=store,    # Enables session summaries + search_memory tool
)
```

When you pass `store` to `build_deep_agent()`, the framework:
1. Automatically adds a `search_memory` tool to the agent
2. Activates `SessionSummaryMiddleware`

### Session Summaries

After every conversation with 5+ messages, the framework automatically:

1. **Summarizes** the session (3–5 sentences via Gemini 2.5 Flash)
2. **Extracts key topics** (comma-separated keywords)
3. **Writes to GCS** at namespace `("shared", "session_summaries")`, key = thread ID
4. **Makes it searchable** via the `search_memory` tool

Example summary document:

```json
{
  "summary": "User asked about deploying to Cloud Run. We walked through the deploy.sh script, fixed a permission error with the service account, and verified the health endpoint was returning 200.",
  "key_topics": ["cloud run", "deployment", "service account", "iam permissions"],
  "thread_id": "alice@company.com",
  "timestamp": "2026-03-18T14:32:00Z",
  "message_count": 12
}
```

### search_memory Tool

The agent can search past sessions using the `search_memory` tool:

```
User: "What did we figure out about the deployment issue last week?"

Agent calls: search_memory(query="deployment issue cloud run")

Returns: 3 matching session summaries ranked by relevance
```

Search uses keyword matching against the `summary` and `key_topics` fields, with boosts for `key_topics` matches.

### GCSStore API

You can also use `GCSStore` directly:

```python
# Store arbitrary documents
store.put(
    namespace=("user", "alice@company.com"),
    key="project-preferences",
    value={
        "preferred_stack": "Python + FastAPI",
        "deploy_target": "Cloud Run",
        "key_topics": ["python", "fastapi", "cloud run"],
    }
)

# Retrieve by key
doc = store.get(namespace=("user", "alice@company.com"), key="project-preferences")

# Search by keyword
results = store.search(
    namespace=("shared", "session_summaries"),
    query="deployment firestore",
    limit=5,
)

# List documents in a namespace
docs = store.list(namespace=("shared", "session_summaries"))
```

**GCS data model:**
```
gs://my-bot-memory/
└── shared/
    └── session_summaries/
        ├── alice@company.com.json
        └── bob@company.com.json
```

---

## Layer 2: Conversation Checkpoints

Checkpoints store the full message history for each conversation thread. This lets the agent remember what was said earlier in the same conversation.

### InMemorySaver (Default — Dev)

```yaml
# bot.yaml / env var: CHECKPOINT_BACKEND=memory
```

- Stored in RAM only
- Lost when the container restarts
- Fine for development and single-user testing

### FirestoreCheckpointSaver (Production)

```bash
# Set in environment
CHECKPOINT_BACKEND=firestore
```

Or set in your deploy script:

```bash
gcloud run services update my-bot \
    --set-env-vars "CHECKPOINT_BACKEND=firestore"
```

- Stored in Firestore at `agent_checkpoints/{thread_id}/checkpoints/{checkpoint_id}`
- Survives container restarts and scaling events
- Supports multiple container instances accessing the same conversation
- Thread IDs are sanitized (`.` and `@` → `-`) for Firestore compatibility

### Thread IDs

The `thread_id` determines which conversation history the agent uses. By convention, use the user's email address:

```python
result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": message}]},
    config={"configurable": {"thread_id": "alice@company.com"}},
)
```

This means each user gets their own isolated conversation history, and the agent remembers everything from previous sessions with that user.

---

## Layer 3: Raw Observations + LLM Council

The LLM Council is an optional async post-processor that classifies raw observation files and builds the `INDEX.md` memory map.

### How It Works

1. **Write raw observations** — Your agent or job handlers can write markdown files to `data/memory/raw/`:
   ```
   data/memory/raw/2026-03-18-deployment.md
   ```

2. **Council processes them** — After each heartbeat cycle (if `HEARTBEAT_COUNCIL_ENABLED=true`), the Council:
   - Reads each raw file
   - Summarizes it (3–5 sentences)
   - Classifies it as `episodic`, `semantic`, `procedural`, or `working`
   - Writes the summary to the appropriate folder
   - Moves the raw file to `raw/processed/`

3. **INDEX.md is updated** — The Council appends new entries to `INDEX.md` with tags and summaries.

### Configuration

```bash
# Enable in environment or bot.yaml
HEARTBEAT_COUNCIL_ENABLED=true
HEARTBEAT_COUNCIL_MODEL=gemini-2.0-flash  # Fast model for classification
```

### Memory Folder Types

| Folder | Type | Contents | Example |
|---|---|---|---|
| `episodic/` | Episodic | Time-stamped events and incidents | "March 1 deployment incident" |
| `semantic/` | Semantic | Facts, knowledge, documentation | "Kubernetes runbook" |
| `procedural/` | Procedural | How-to guides and checklists | "Deploy checklist" |
| `working/` | Working | Current task context | "Active sprint items" |

### Custom Folders

Add custom classification categories:

```python
from emonk.core.config import CustomMemoryFolder

custom_folders = [
    CustomMemoryFolder(name="customer-notes", description="Observations about specific customers"),
    CustomMemoryFolder(name="bugs", description="Bug reports and known issues"),
]

agent = build_deep_agent(
    model=model,
    tools=tools,
    store=store,
    council_custom_folders=custom_folders,
)
```

---

## GCS Filesystem Sync

`GCSFilesystemSync` keeps your agent's local `data/memory/` directory in sync with a GCS bucket. This is critical for production deployments where containers may be replaced, scaled, or cold-started.

### Sync Strategy

| Event | Action |
|---|---|
| Container starts | Pull everything from GCS → local |
| Every 5 minutes | Push local changes → GCS |
| Container shuts down (SIGTERM) | Final push local → GCS |

### Configuration

```yaml
# bot.yaml
memory:
  backend: gcs
  bucket: my-bot-memory
```

The sync runs automatically when `build_deep_agent()` detects `MEMORY_BACKEND=gcs`. You need to wire the lifespan correctly:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start GCS sync (pulls from GCS on startup)
    if hasattr(agent, 'fs_sync') and agent.fs_sync:
        await agent.fs_sync.start()
    yield
    # Final push to GCS on shutdown
    if hasattr(agent, 'fs_sync') and agent.fs_sync:
        await agent.fs_sync.close()

app = FastAPI(lifespan=lifespan)
```

### GCS Bucket Structure

```
gs://my-bot-memory/
└── data/
    └── memory/
        ├── SOUL.md
        ├── IDENTITY.md
        ├── USER.md
        ├── INDEX.md
        ├── scheduler/
        │   └── jobs.json
        ├── episodic/
        │   └── 2026-03-01-incident.md
        ├── semantic/
        │   └── kubernetes-runbook.md
        ├── raw/
        │   ├── 2026-03-18-observation.md
        │   └── processed/
        └── shared/
            └── session_summaries/
```

---

## Memory Best Practices

### Keep identity files focused

Write `SOUL.md` and `IDENTITY.md` as if briefing a new employee: essential context only. Verbose files eat into the token budget available for actual conversation.

### Use descriptive file names

Raw observation files with dates and topics (e.g., `2026-03-18-api-rate-limit-issue.md`) make the INDEX more useful and easier for the agent to reason about.

### Don't duplicate in INDEX.md

The LLM Council maintains `INDEX.md` automatically. If you write it manually, keep entries concise — the agent reads the full file on startup.

### Use Firestore checkpoints in production

`InMemorySaver` is fine locally but means users lose conversation context on every cold start. Switch to Firestore for any bot with more than one user or real workloads.

### GCS memory is eventually consistent

The 5-minute sync interval means a container restart within 5 minutes could lose recent memory writes. For critical data (like scheduled jobs), use Firestore directly.
