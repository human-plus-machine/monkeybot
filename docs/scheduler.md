# Scheduler & Background Jobs

monkey-bot includes a built-in cron scheduler that lets your agent create and execute background jobs — reports, reminders, alerts, data pipelines, or anything else that needs to run at a future time.

---

## Overview

```
User: "Schedule a report for Friday at 9am"
          │
          ▼
Agent calls: schedule_task(
    job_type="weekly_report",
    schedule_at_iso="2026-03-20T09:00:00-05:00",
    payload={"user_email": "alice@company.com", "space_name": "spaces/ABC"}
)
          │
          ▼
CronScheduler stores job in JSON / Firestore
          │
          ▼ (Friday 9am)
Cloud Scheduler → POST /cron/tick
          │
          ▼
CronScheduler.run_tick()
    - Loads all pending jobs
    - Finds jobs where schedule_at <= now
    - Claims the job (distributed lock)
    - Calls handle_weekly_report(job)
          │
          ▼
handle_weekly_report posts results to Google Chat
```

---

## Core Concepts

### Jobs

A job has:

| Field | Type | Description |
|---|---|---|
| `id` | UUID string | Unique identifier |
| `job_type` | string | Maps to a registered handler |
| `schedule_at` | ISO 8601 string | When to execute |
| `payload` | dict | Arbitrary data passed to the handler |
| `status` | string | `pending`, `running`, `completed`, `failed` |
| `attempts` | int | Number of execution attempts |
| `max_attempts` | int | Max retries before marking failed (default: 3) |
| `created_at` | ISO 8601 string | When the job was created |

### Job Handlers

Handlers are async functions registered with the scheduler. One handler per `job_type`.

### Storage Backends

| Backend | Use Case | Locking |
|---|---|---|
| `json` | Local dev, single-instance | None (always claims) |
| `firestore` | Production, multi-instance | Atomic Firestore transactions |

---

## Setup

### 1. Create the Scheduler

```python
from emonk.core.scheduler import CronScheduler, create_storage

# create_storage() reads SCHEDULER_STORAGE from bot.yaml/env
storage = create_storage()
scheduler = CronScheduler(storage=storage)
```

Or with explicit storage:

```python
from emonk.core.scheduler.storage import JSONFileStorage, FirestoreStorage

# Development
storage = JSONFileStorage(memory_dir="./data/memory")

# Production
storage = FirestoreStorage(project_id="my-gcp-project")

scheduler = CronScheduler(storage=storage)
```

### 2. Pass to build_deep_agent

```python
agent = build_deep_agent(
    model=model,
    tools=tools,
    user_system_prompt=prompt,
    scheduler=scheduler,   # Auto-adds schedule_task tool
)

# Required: attach scheduler to agent so /cron/tick can reach it
agent.scheduler = scheduler
```

### 3. Register Job Handlers

```python
# src/job_handlers.py
import httpx
import os

async def handle_weekly_report(job: dict) -> None:
    """Generate and post a weekly report."""
    payload = job.get("payload", {})
    user_email = payload.get("user_email", "unknown")
    space_name = payload.get("space_name")
    
    # Your report generation logic
    report_lines = [
        "📊 *Weekly Report*",
        f"Generated for: {user_email}",
        "• 42 API calls processed",
        "• 0 errors in the last 7 days",
        "• 3 scheduled jobs completed",
    ]
    report_text = "\n".join(report_lines)
    
    # Post to Google Chat
    webhook_url = os.getenv("GOOGLE_CHAT_WEBHOOK")
    if webhook_url:
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json={"text": report_text})

async def handle_reminder(job: dict) -> None:
    """Send a reminder message."""
    payload = job.get("payload", {})
    message = payload.get("message", "Reminder!")
    
    webhook_url = os.getenv("GOOGLE_CHAT_WEBHOOK")
    if webhook_url:
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json={"text": message})

def register_handlers(scheduler: CronScheduler) -> None:
    """Register all job type → handler mappings."""
    scheduler.register_handler("weekly_report", handle_weekly_report)
    scheduler.register_handler("reminder", handle_reminder)
```

```python
# src/main.py
from src.job_handlers import register_handlers

# ... after agent is created ...
register_handlers(scheduler)
```

---

## The schedule_task Tool

When you pass `scheduler` to `build_deep_agent()`, the framework automatically adds a `schedule_task` tool. Users can ask the agent to schedule jobs, and the agent calls this tool.

**Tool signature:**

```python
async def schedule_task(
    job_type: str,         # Must match a registered handler
    schedule_at_iso: str,  # ISO 8601 datetime with timezone
    payload: dict,         # Arbitrary data for the handler
) -> str
```

**Example conversation:**

```
User: "Send me a reminder in 30 minutes to check the deployment logs"

Agent calls: schedule_task(
    job_type="reminder",
    schedule_at_iso="2026-03-18T15:30:00-05:00",
    payload={
        "message": "⏰ Reminder: Check the deployment logs!",
        "user_email": "alice@company.com"
    }
)

Agent responds: "Done! I'll remind you at 3:30 PM to check the deployment logs."
```

---

## The /cron/tick Endpoint

Cloud Scheduler calls `POST /cron/tick` on the configured cadence. This triggers `CronScheduler.run_tick()`.

### What run_tick() does

```python
async def run_tick() -> dict:
    """Execute one tick of the scheduler."""
    jobs = await storage.load_jobs()
    now = datetime.now(timezone.utc)
    
    for job in jobs:
        if job["status"] != "pending":
            continue
        if parse_datetime(job["schedule_at"]) > now:
            continue
        
        # Claim the job (distributed lock)
        claimed = await storage.claim_job(job["id"])
        if not claimed:
            continue  # Another instance got it
        
        # Execute
        handler = registered_handlers[job["job_type"]]
        await handler(job)
        
        # Mark complete or failed (with retry logic)
    
    return {"checked": N, "due": M, "executed": K, "succeeded": J}
```

### Security

Protect `/cron/tick` from unauthorized calls by setting `CRON_SECRET`:

```bash
# .env or GCP Secret Manager
CRON_SECRET=your-random-secret-here
```

When set, the endpoint requires:

```
Authorization: Bearer your-random-secret-here
```

Cloud Scheduler sends an OIDC token by default (configured in `setup-scheduler.sh`), so this is an additional layer.

### Local Testing

Test the scheduler without Cloud Scheduler by calling the endpoint directly:

```bash
curl -X POST http://localhost:8080/cron/tick \
    -H "Authorization: Bearer your-cron-secret"
```

Or trigger it manually in code:

```python
result = await scheduler.run_tick()
print(result)
# {"checked": 5, "due": 2, "executed": 2, "succeeded": 2}
```

---

## Storage Backends

### JSONFileStorage (Dev)

Jobs are stored as a JSON array in `{memory_dir}/scheduler/jobs.json`.

```json
[
  {
    "id": "abc123",
    "job_type": "weekly_report",
    "schedule_at": "2026-03-20T09:00:00-05:00",
    "payload": {"user_email": "alice@company.com"},
    "status": "pending",
    "attempts": 0,
    "max_attempts": 3,
    "created_at": "2026-03-18T10:00:00Z"
  }
]
```

**When to use:** Local development, single-container deployments where job loss on restart is acceptable.

**Limitation:** No distributed locking — if multiple instances run simultaneously, the same job can execute twice.

### FirestoreStorage (Production)

Jobs are stored as Firestore documents in the `scheduler_jobs` collection.

```
scheduler_jobs/
└── abc123 (document)
    ├── id: "abc123"
    ├── job_type: "weekly_report"
    ├── schedule_at: "2026-03-20T09:00:00-05:00"
    ├── payload: {...}
    ├── status: "pending"
    ├── attempts: 0
    └── lease_expires_at: null
```

Locking uses `@firestore.transactional` to atomically:
1. Read the job's current status
2. Check if it's unclaimed and due
3. Set `status = "running"` and `lease_expires_at = now + 5 minutes`

If the container crashes mid-execution, the lease expires and the job becomes available for retry.

**Setup:**

```bash
# Create Firestore database
gcloud firestore databases create --location=us-central1 --project=your-project
```

```yaml
# bot.yaml
scheduler:
  storage: firestore
```

---

## Retry Behavior

Jobs automatically retry on failure:

| Attempt | Retry Delay |
|---|---|
| 1st failure | Retry in 5 minutes |
| 2nd failure | Retry in 5 minutes |
| 3rd failure | Mark as `failed`, no more retries |

To change `max_attempts`:

```python
# When scheduling programmatically (not via the agent tool)
await scheduler.schedule_job(
    job_type="weekly_report",
    schedule_at=datetime.now(timezone.utc) + timedelta(hours=1),
    payload={"user_email": "alice@company.com"},
    max_attempts=5,  # Override default of 3
)
```

---

## Heartbeat Jobs

The Heartbeat system is a special built-in job type that makes the agent periodically check in with its own workspace and send a status report.

### What it does

On each heartbeat cycle:
1. Reads `HEARTBEAT.md` (workspace check instructions) + `IDENTITY.md` + `INDEX.md`
2. Invokes the agent with a prompt: "review your workspace, is there anything urgent?"
3. Agent responds with `URGENT: yes|no` and a `SUMMARY:` line
4. Posts the report to Google Chat (format: standup, detailed, or minimal)
5. If urgent and within active hours, sends an additional alert
6. Optionally runs LLM Council to process raw memory files

### Configuration

```bash
HEARTBEAT_ENABLED=true
HEARTBEAT_CRON=*/30 * * * *         # Every 30 minutes
HEARTBEAT_ACTIVE_HOURS_START=09:00  # 9am
HEARTBEAT_ACTIVE_HOURS_END=18:00    # 6pm
HEARTBEAT_ACTIVE_HOURS_TZ=America/New_York
HEARTBEAT_NOTIFY_ON_COMPLETE=true
HEARTBEAT_NOTIFY_FORMAT=standup     # standup | detailed | minimal
HEARTBEAT_COUNCIL_ENABLED=true      # Run LLM Council after each heartbeat
```

### HEARTBEAT.md

Create this file in your memory directory to customize what the agent checks:

```markdown
<!-- data/memory/HEARTBEAT.md -->
During your workspace check, review:
1. Any files modified in the last 30 minutes in data/memory/raw/
2. Check if there are any pending items in INDEX.md marked as urgent
3. Review whether any scheduled jobs have failed (check scheduler logs)

Respond with:
- URGENT: yes/no (is there anything requiring immediate attention?)
- SUMMARY: 1-2 sentences about the current state of your workspace
```

### Notify formats

**standup:**
```
🐵 monkey-bot standup
Status: All good
Memory: 3 new observations indexed
Jobs: 2 completed, 0 failed
```

**detailed:**
```
🐵 monkey-bot heartbeat report
[Full agent response with all context]
```

**minimal:**
```
✅ monkey-bot: OK
```

---

## Scheduling Patterns

### One-time job

```python
from datetime import datetime, timezone, timedelta

# Schedule for 2 hours from now
schedule_at = datetime.now(timezone.utc) + timedelta(hours=2)

await scheduler.schedule_job(
    job_type="send_report",
    schedule_at=schedule_at,
    payload={"user_email": "alice@company.com"},
)
```

### Via the agent tool (from user request)

The agent handles time parsing. Users can say:
- "tomorrow at 9am"
- "in 2 hours"
- "every Monday at 9am" *(note: single-fire jobs only; recurring jobs require re-scheduling)*
- "March 20th at 3pm EST"

### Programmatic recurring jobs

For truly recurring jobs, have the handler re-schedule itself:

```python
async def handle_daily_digest(job: dict) -> None:
    """Send daily digest and reschedule for tomorrow."""
    # ... generate and send digest ...
    
    # Reschedule for same time tomorrow
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    await scheduler.schedule_job(
        job_type="daily_digest",
        schedule_at=tomorrow,
        payload=job.get("payload", {}),
    )
```

---

## Monitoring

### Check job status via logs

```bash
# GCP Cloud Run
gcloud run services logs read my-bot \
    --region us-central1 \
    --filter="textPayload~scheduler" \
    --limit 50

# Look for:
# INFO: Scheduler tick: 3 checked, 1 due, 1 executed, 1 succeeded
# ERROR: Job abc123 failed (attempt 1/3): <error message>
```

### Inspect jobs directly

```bash
# Local (JSON storage)
cat ./data/memory/scheduler/jobs.json | python -m json.tool

# Production (Firestore)
gcloud firestore documents list \
    "projects/my-project/databases/(default)/documents/scheduler_jobs"
```

### Trigger manually

```bash
# Force run all due jobs immediately
curl -X POST https://my-bot.run.app/cron/tick \
    -H "Authorization: Bearer ${CRON_SECRET}"
```

---

## Cloud Scheduler Setup

See [Deploy to GCP → Cloud Scheduler](deploy-gcp.md#part-7-set-up-cloud-scheduler) for the full setup. Quick reference:

```bash
# One-command setup using the included script
cd test-monkey
./setup-scheduler.sh

# Or manually:
gcloud scheduler jobs create http my-bot-tick \
    --location us-central1 \
    --schedule "* * * * *" \
    --uri "https://my-bot.run.app/cron/tick" \
    --http-method POST \
    --oidc-service-account-email "my-bot-tick@project.iam.gserviceaccount.com" \
    --project my-gcp-project
```
