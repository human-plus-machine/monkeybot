# Configuration Reference

monkey-bot uses a two-file configuration system:

- **`bot.yaml`** — Non-secret configuration. Committed to git. Baked into Docker image.
- **`.env`** — Secrets for local development. Never committed.

In production, secrets come from GCP Secret Manager (or AWS Secrets Manager, coming soon).

**Priority order (highest wins):** Environment variables → `bot.yaml` → Framework defaults

---

## bot.yaml — Full Reference

```yaml
# =============================================================================
# Agent
# =============================================================================
agent:
  name: my-bot           # Display name used in logs and heartbeat reports
  skills_dir: ./skills   # Directory where the framework looks for SKILL.md files

# =============================================================================
# Model
# =============================================================================
model:
  # Provider: google_vertexai | openai | anthropic | vertex_anthropic
  provider: google_vertexai
  
  # Model name (provider-specific)
  # google_vertexai: gemini-2.5-flash, gemini-2.5-pro, gemini-2.0-flash
  # openai: gpt-4o, gpt-4o-mini, gpt-4-turbo
  # anthropic: claude-3-5-sonnet-20241022, claude-3-5-haiku-20241022
  name: gemini-2.5-flash
  
  temperature: 0.7       # 0.0 = deterministic, 1.0 = highly creative
  max_tokens: 8192       # Maximum output tokens per response
  
  # Thinking budget for models that support extended reasoning
  # -1 = dynamic (model decides), 0 = disabled, N = max thinking tokens
  thinking_budget: -1

# =============================================================================
# Server
# =============================================================================
server:
  port: 8080             # HTTP port the FastAPI app listens on
  log_level: INFO        # DEBUG | INFO | WARNING | ERROR

# =============================================================================
# Gateway
# =============================================================================
gateway:
  # Users allowed to interact with the bot via /webhook.
  # YAML list format (preferred) or comma-separated string.
  allowed_users:
    - alice@company.com
    - bob@company.com
  
  # Response format for /webhook endpoint
  # workspace_addon: Google Chat Workspace Add-on format (recommended)
  # legacy: Simple {"text": "..."} format
  chat_format: workspace_addon

# =============================================================================
# Memory
# =============================================================================
memory:
  dir: ./data/memory     # Local directory for memory files
  
  # Storage backend
  # local: Files only live on the container (lost on restart without GCS sync)
  # gcs: Files synced to/from a GCS bucket on startup/shutdown
  backend: local
  
  # GCS bucket name (required if backend: gcs)
  bucket: my-bot-memory

# =============================================================================
# Scheduler
# =============================================================================
scheduler:
  # Job storage backend
  # json: Stored as ./data/memory/scheduler/jobs.json
  #       Dev only — no distributed locking, not safe for multi-instance
  # firestore: Stored in Firestore with atomic lease-based locking
  #            Safe for production and multi-instance deployments
  storage: json
  
  # How often Cloud Scheduler calls /cron/tick
  # Standard cron expression (minute hour day month weekday)
  # "* * * * *"     = every minute
  # "*/5 * * * *"   = every 5 minutes
  # "0 * * * *"     = every hour
  # "0 9 * * 1-5"   = weekdays at 9am
  cadence: "* * * * *"
  
  timezone: America/New_York   # IANA timezone name

# =============================================================================
# Secrets
# =============================================================================
secrets:
  # Secrets provider
  # env: Load from .env file (local development)
  # gcp_secret_manager: Load from GCP Secret Manager (production)
  provider: env

# =============================================================================
# Cloud Providers
# Only configure the provider(s) you're actually using.
# =============================================================================

# Google Cloud Platform
gcp:
  project_id: my-gcp-project    # Required for Vertex AI, GCS, Firestore, Secret Manager
  location: us-central1          # GCP region

# Amazon Web Services (coming soon)
# aws:
#   region: us-east-1
#   account_id: "123456789012"

# Microsoft Azure (coming soon)
# azure:
#   subscription_id: "..."
#   resource_group: "..."
```

---

## Environment Variables — Full Reference

Every `bot.yaml` setting can be overridden by the corresponding environment variable.

### Agent

| Variable | Default | Description |
|---|---|---|
| `AGENT_NAME` | `monkey-bot` | Agent display name |
| `SKILLS_DIR` | `./skills` | Path to skills directory |
| `ENVIRONMENT` | `development` | `development` or `production` |

### Model

| Variable | Default | Description |
|---|---|---|
| `MODEL_PROVIDER` | `google_vertexai` | `google_vertexai`, `openai`, `anthropic`, `vertex_anthropic` |
| `MODEL_NAME` | `gemini-2.5-flash` | Model identifier for the chosen provider |
| `MODEL_TEMPERATURE` | `0.7` | Generation temperature (0.0–1.0) |
| `MODEL_MAX_TOKENS` | `8192` | Max output tokens per response |
| `MODEL_THINKING_BUDGET` | `-1` | Extended reasoning token budget |

### Server

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP port |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Gateway

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_USERS` | — | Comma-separated list of authorized emails |
| `GOOGLE_CHAT_FORMAT` | `workspace_addon` | `workspace_addon` or `legacy` |
| `GOOGLE_CHAT_WEBHOOK` | — | Incoming webhook URL for posting outbound messages |

### Memory

| Variable | Default | Description |
|---|---|---|
| `MEMORY_DIR` | `./data/memory` | Local memory directory path |
| `MEMORY_BACKEND` | `local` | `local` or `gcs` |
| `GCS_MEMORY_BUCKET` | — | GCS bucket name (required if `MEMORY_BACKEND=gcs`) |
| `GCS_ENABLED` | `false` | Derived from `MEMORY_BACKEND`; set to `true` to enable GCS |

### Scheduler

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_STORAGE` | `json` | `json` or `firestore` |
| `SCHEDULER_CADENCE` | `* * * * *` | Cron expression |
| `SCHEDULER_TIMEZONE` | `America/New_York` | IANA timezone name |
| `CRON_SECRET` | — | Optional Bearer token for securing `/cron/tick` |

### Secrets

| Variable | Default | Description |
|---|---|---|
| `SECRETS_PROVIDER` | `env` | `env` or `gcp_secret_manager` |

### GCP

| Variable | Default | Description |
|---|---|---|
| `GCP_PROJECT_ID` | — | GCP project ID |
| `VERTEX_AI_PROJECT_ID` | — | GCP project ID for Vertex AI (usually same as `GCP_PROJECT_ID`) |
| `VERTEX_AI_LOCATION` | `us-central1` | Vertex AI region |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to service account JSON (local dev only) |

### Checkpointing

| Variable | Default | Description |
|---|---|---|
| `CHECKPOINT_BACKEND` | `memory` | `memory` (dev) or `firestore` (production) |

### Heartbeat

| Variable | Default | Description |
|---|---|---|
| `HEARTBEAT_ENABLED` | `false` | Enable scheduled self-check jobs |
| `HEARTBEAT_CRON` | `*/30 * * * *` | How often to run heartbeats |
| `HEARTBEAT_ACTIVE_HOURS_START` | `09:00` | Start of active hours (HH:MM) |
| `HEARTBEAT_ACTIVE_HOURS_END` | `18:00` | End of active hours (HH:MM) |
| `HEARTBEAT_ACTIVE_HOURS_TZ` | `America/New_York` | Timezone for active hours |
| `HEARTBEAT_TARGET` | `last_active` | Who to notify: `last_active` |
| `HEARTBEAT_IDENTITY_FILE` | — | Path to `IDENTITY.md` for heartbeat context |
| `HEARTBEAT_NOTIFY_ON_COMPLETE` | `true` | Post a report after each heartbeat |
| `HEARTBEAT_NOTIFY_FORMAT` | `standup` | `standup`, `detailed`, or `minimal` |
| `HEARTBEAT_COUNCIL_ENABLED` | `false` | Run LLM Council memory processing after heartbeat |
| `HEARTBEAT_COUNCIL_MODEL` | `gemini-2.0-flash` | Model for memory classification |

### Voice

| Variable | Default | Description |
|---|---|---|
| `VOICE_ENABLED` | `false` | Enable `/voice` endpoint |
| `VOICE_STT_LANGUAGE_CODE` | `en-US` | Speech-to-Text language |
| `VOICE_STT_MODEL` | `latest_long` | STT model (`latest_long`, `latest_short`, `telephony`) |
| `VOICE_TTS_VOICE_NAME` | `en-US-Journey-F` | Text-to-Speech voice |
| `VOICE_TTS_AUDIO_ENCODING` | `OGG_OPUS` | Audio encoding (`OGG_OPUS`, `MP3`, `LINEAR16`) |

---

## .env File Reference

The `.env` file is for secrets only — values that can't be committed to git.

```bash
# =============================================================================
# GCP Credentials (local dev only)
# =============================================================================
# Path to your GCP service account JSON key file.
# In production (Cloud Run), this is not needed — Cloud Run uses the
# service account automatically via the metadata server.
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# GCP Project ID (required for Vertex AI)
VERTEX_AI_PROJECT_ID=your-gcp-project-id

# =============================================================================
# Optional Secrets
# =============================================================================

# Google Chat incoming webhook URL
# Create at: Google Chat → Space → Apps & integrations → Manage webhooks
GOOGLE_CHAT_WEBHOOK=https://chat.googleapis.com/v1/spaces/.../messages?key=...

# Cron endpoint security token
# If set, /cron/tick requires: Authorization: Bearer <CRON_SECRET>
CRON_SECRET=your-random-secret-here

# =============================================================================
# Override any bot.yaml setting locally
# =============================================================================

# Use a different model locally without changing bot.yaml
# MODEL_NAME=gemini-2.0-flash
# MODEL_TEMPERATURE=0.3

# Enable GCS memory locally (if you want to test against real GCS)
# MEMORY_BACKEND=gcs
# GCS_MEMORY_BUCKET=dev-memory-bucket
```

---

## GCP Secrets Reference

When `secrets.provider: gcp_secret_manager`, monkey-bot loads the following secrets from Secret Manager at startup. Secret names follow the pattern `your-secret-name` (lowercase with hyphens).

| Secret Name | Required | Maps To | Example Value |
|---|---|---|---|
| `vertex-ai-project-id` | Yes | `VERTEX_AI_PROJECT_ID` | `my-gcp-project` |
| `allowed-users` | Yes | `ALLOWED_USERS` | `alice@co.com,bob@co.com` |
| `google-chat-webhook` | Optional | `GOOGLE_CHAT_WEBHOOK` | `https://chat.googleapis.com/...` |
| `cron-secret` | Optional | `CRON_SECRET` | `abc123...` |
| `openai-api-key` | If `MODEL_PROVIDER=openai` | `OPENAI_API_KEY` | `sk-...` |
| `anthropic-api-key` | If `MODEL_PROVIDER=anthropic` | `ANTHROPIC_API_KEY` | `sk-ant-...` |

To add a secret:

```bash
echo -n "secret-value" | gcloud secrets create secret-name \
    --data-file=- \
    --replication-policy="automatic" \
    --project=your-project
```

To update a secret:

```bash
echo -n "new-value" | gcloud secrets versions add secret-name \
    --data-file=- \
    --project=your-project
```

---

## Model Provider Configuration

### Google Vertex AI (Default)

```yaml
model:
  provider: google_vertexai
  name: gemini-2.5-flash   # or gemini-2.5-pro, gemini-2.0-flash
```

Requires:
- `VERTEX_AI_PROJECT_ID`
- `GOOGLE_APPLICATION_CREDENTIALS` (local) or service account with `roles/aiplatform.user` (Cloud Run)

Available models:
| Model | Best For | Speed | Cost |
|---|---|---|---|
| `gemini-2.5-flash` | Most tasks, default choice | Fast | Low |
| `gemini-2.5-pro` | Complex reasoning tasks | Moderate | Higher |
| `gemini-2.0-flash` | High-volume, low latency | Very Fast | Very Low |

### OpenAI

```yaml
model:
  provider: openai
  name: gpt-4o             # or gpt-4o-mini, gpt-4-turbo
```

Requires:
- `OPENAI_API_KEY` in `.env` or Secret Manager

### Anthropic Claude

```yaml
model:
  provider: anthropic
  name: claude-3-5-sonnet-20241022  # or claude-3-5-haiku-20241022
```

Requires:
- `ANTHROPIC_API_KEY` in `.env` or Secret Manager

### Anthropic via Vertex AI

```yaml
model:
  provider: vertex_anthropic
  name: claude-3-5-sonnet-v2@20241022
```

Requires:
- `VERTEX_AI_PROJECT_ID` and Vertex AI service account
- Anthropic model access enabled in Vertex AI Model Garden

---

## Configuration Validation

monkey-bot validates your configuration at startup and will fail fast with clear error messages if required values are missing or invalid.

```bash
# Test config loading locally
python -c "from emonk.core.config import load_bot_config; config = load_bot_config(); print(config)"
```

Common validation errors:

| Error | Cause | Fix |
|---|---|---|
| `VERTEX_AI_PROJECT_ID is required` | Missing GCP project | Set in `.env` or Secret Manager |
| `Unknown model provider: xyz` | Typo in `model.provider` | Use `google_vertexai`, `openai`, or `anthropic` |
| `GCS_MEMORY_BUCKET is required when MEMORY_BACKEND=gcs` | Missing bucket name | Set `memory.bucket` in `bot.yaml` |
| `Invalid cron expression` | Bad scheduler cadence | Validate at https://crontab.guru |
