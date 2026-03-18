# Integrations

monkey-bot is designed to be connected to everything. This page documents all supported integrations — active, beta, and coming soon.

---

## LLM Providers

### Google Vertex AI (Gemini) — Default

The default LLM provider. Gemini 2.5 Flash is recommended for most use cases.

**Configuration:**

```yaml
# bot.yaml
model:
  provider: google_vertexai
  name: gemini-2.5-flash   # gemini-2.5-pro | gemini-2.0-flash
```

**Required:**
- GCP project with Vertex AI API enabled
- Service account with `roles/aiplatform.user`

```bash
# Enable Vertex AI API
gcloud services enable aiplatform.googleapis.com --project=your-project

# Grant access
gcloud projects add-iam-policy-binding your-project \
    --member="serviceAccount:your-sa@your-project.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"
```

**Available models:**

| Model | Best For | Context Window |
|---|---|---|
| `gemini-2.5-flash` | Balanced speed/quality, default | 1M tokens |
| `gemini-2.5-pro` | Complex reasoning, analysis | 2M tokens |
| `gemini-2.0-flash` | High throughput, low latency | 1M tokens |

---

### OpenAI

**Configuration:**

```yaml
# bot.yaml
model:
  provider: openai
  name: gpt-4o   # gpt-4o-mini | gpt-4-turbo | gpt-3.5-turbo
```

**Required:**
- `OPENAI_API_KEY` in `.env` (dev) or Secret Manager (production)

```bash
# Add to GCP Secret Manager
echo -n "sk-..." | gcloud secrets create openai-api-key \
    --data-file=- --project=your-project
```

**Available models:**

| Model | Best For |
|---|---|
| `gpt-4o` | Best quality, multimodal |
| `gpt-4o-mini` | Faster, lower cost |
| `gpt-4-turbo` | Long-context tasks |

---

### Anthropic Claude

**Configuration:**

```yaml
# bot.yaml
model:
  provider: anthropic
  name: claude-3-5-sonnet-20241022  # claude-3-5-haiku-20241022 | claude-3-opus-20240229
```

**Required:**
- `ANTHROPIC_API_KEY` in `.env` (dev) or Secret Manager (production)

---

### Anthropic via Vertex AI

Run Claude models on Google infrastructure — useful for data residency requirements.

**Configuration:**

```yaml
# bot.yaml
model:
  provider: vertex_anthropic
  name: claude-3-5-sonnet-v2@20241022
```

**Required:**
- Vertex AI API enabled
- Anthropic model access in Vertex AI Model Garden
- Service account with `roles/aiplatform.user`

---

### Amazon Bedrock — Coming Soon

Native integration with Bedrock for AWS deployments.

```yaml
# Coming soon
model:
  provider: aws_bedrock
  name: anthropic.claude-3-5-sonnet-20241022-v2:0
```

---

## Messaging Platforms

### Google Chat (Workspace Add-on)

The primary user interface. Your bot lives inside Google Chat as a Workspace Add-on and responds to direct messages.

**Configuration:**

```yaml
# bot.yaml
gateway:
  allowed_users:
    - alice@company.com
  chat_format: workspace_addon
```

**Setup:**

1. Enable Google Chat API: `https://console.cloud.google.com/apis/api/chat.googleapis.com`
2. Set **HTTP Endpoint URL** to `https://your-bot.run.app/webhook`
3. Add the bot to a Google Chat space
4. Users message `@your-bot their question`

**Webhook payload structure:**

```json
{
  "chat": {
    "messagePayload": {
      "message": {
        "sender": {"email": "alice@company.com"},
        "argumentText": "run diagnostics"
      },
      "space": {
        "name": "spaces/ABC123",
        "type": "ROOM"
      }
    }
  }
}
```

**Response format (workspace_addon):**

```json
{
  "hostAppDataAction": {
    "chatDataAction": {
      "createMessageAction": {
        "message": {"text": "Your response here"}
      }
    }
  }
}
```

**Response format (legacy):**

```json
{"text": "Your response here"}
```

**Google Chat outbound webhook (for posting scheduled results):**

1. Google Chat → Space → Apps & integrations → Manage webhooks → Add webhook
2. Copy the URL
3. Store as `GOOGLE_CHAT_WEBHOOK` in Secret Manager

```bash
echo -n "https://chat.googleapis.com/..." | gcloud secrets create google-chat-webhook \
    --data-file=- --project=your-project
```

---

### Slack — Coming Soon

Direct message and channel bot with slash command support.

---

### Microsoft Teams — Coming Soon

Teams app integration with adaptive cards.

---

### Telegram — Coming Soon

Telegram bot with inline keyboard and command support.

---

## Cloud Storage

### Google Cloud Storage (GCS)

Used for:
- Long-term agent memory (`GCSStore`)
- Memory directory sync (`GCSFilesystemSync`)

**Configuration:**

```yaml
# bot.yaml
memory:
  backend: gcs
  bucket: my-bot-memory
```

**Setup:**

```bash
# Create bucket
gcloud storage buckets create gs://my-bot-memory \
    --location=us-central1 \
    --uniform-bucket-level-access

# Grant service account access
gcloud storage buckets add-iam-policy-binding gs://my-bot-memory \
    --member="serviceAccount:your-sa@project.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

**Code:**

```python
from emonk.core.store import GCSStore

store = GCSStore(
    bucket_name="my-bot-memory",
    project_id="my-gcp-project",
)

agent = build_deep_agent(model=model, tools=tools, store=store)
```

---

### Amazon S3 — Coming Soon

Memory backend for AWS deployments.

---

### Azure Blob Storage — Coming Soon

Memory backend for Azure deployments.

---

## Databases

### Google Cloud Firestore

Used for:
- Conversation checkpoints (`FirestoreCheckpointSaver`)
- Distributed scheduler job storage (`FirestoreStorage`)

**Configuration:**

```bash
# Environment variables
CHECKPOINT_BACKEND=firestore
SCHEDULER_STORAGE=firestore  # via bot.yaml
```

```yaml
# bot.yaml
scheduler:
  storage: firestore
```

**Setup:**

```bash
# Create Firestore database
gcloud firestore databases create \
    --location=us-central1 \
    --project=your-project

# Grant service account access
gcloud projects add-iam-policy-binding your-project \
    --member="serviceAccount:your-sa@your-project.iam.gserviceaccount.com" \
    --role="roles/datastore.user"
```

**Data model:**

```
# Checkpoints
agent_checkpoints/{thread_id}/checkpoints/{checkpoint_id}

# Scheduler jobs
scheduler_jobs/{job_id}
```

---

### Amazon DynamoDB — Coming Soon

Scheduler job storage for AWS deployments.

---

### Azure CosmosDB — Coming Soon

Scheduler job storage for Azure deployments.

---

## Secrets Management

### GCP Secret Manager

Used in production to store all sensitive configuration values.

**Configuration:**

```yaml
# bot.yaml
secrets:
  provider: gcp_secret_manager
```

**Secrets loaded automatically:**

| Secret Name | Maps To |
|---|---|
| `vertex-ai-project-id` | `VERTEX_AI_PROJECT_ID` |
| `allowed-users` | `ALLOWED_USERS` |
| `google-chat-webhook` | `GOOGLE_CHAT_WEBHOOK` |
| `cron-secret` | `CRON_SECRET` |
| `openai-api-key` | `OPENAI_API_KEY` |
| `anthropic-api-key` | `ANTHROPIC_API_KEY` |

**Setup:**

```bash
# Create a secret
echo -n "value" | gcloud secrets create secret-name \
    --data-file=- \
    --replication-policy="automatic" \
    --project=your-project

# Update a secret
echo -n "new-value" | gcloud secrets versions add secret-name \
    --data-file=- \
    --project=your-project
```

---

### AWS Secrets Manager — Coming Soon

Secrets provider for AWS deployments.

---

### Azure Key Vault — Coming Soon

Secrets provider for Azure deployments.

---

## Hosting & Compute

### Google Cloud Run

The recommended production hosting platform. Fully managed, serverless, autoscales to zero.

**Deploy:**

```bash
./deploy.sh
```

See [Deploy to GCP](deploy-gcp.md) for the full guide.

**Key settings:**

```bash
gcloud run deploy my-bot \
    --memory 512Mi \
    --cpu 1 \
    --max-instances 10 \
    --min-instances 0 \          # Set to 1 to eliminate cold starts
    --concurrency 1 \            # 1 request per instance (LLM workloads)
    --timeout 300                # 5 minute request timeout
```

---

### Amazon ECS (Fargate) — Coming Soon

AWS-native serverless container hosting.

---

### Azure Container Apps — Coming Soon

Azure-native serverless container hosting.

---

### Kubernetes — Coming Soon

Self-hosted deployments via Helm chart.

---

## Scheduling

### GCP Cloud Scheduler

Triggers `POST /cron/tick` on the configured cadence to run background jobs.

**Setup:**

```bash
gcloud scheduler jobs create http my-bot-tick \
    --location us-central1 \
    --schedule "* * * * *" \
    --uri "https://my-bot.run.app/cron/tick" \
    --http-method POST \
    --oidc-service-account-email "my-bot-tick@project.iam.gserviceaccount.com" \
    --project my-project
```

Or use the included setup script:

```bash
./setup-scheduler.sh
```

See [Scheduler & Jobs](scheduler.md) for full details.

---

### AWS EventBridge Scheduler — Coming Soon

AWS-native scheduled triggers for ECS deployments.

---

## Voice

### GCP Speech-to-Text

Converts audio input at the `/voice` endpoint to text for the agent.

**Configuration:**

```bash
VOICE_ENABLED=true
VOICE_STT_LANGUAGE_CODE=en-US
VOICE_STT_MODEL=latest_long
```

**Required:**
- Google Cloud Speech-to-Text API enabled
- `google-cloud-speech` Python package: `pip install "emonk[voice]"`

---

### GCP Text-to-Speech

Converts the agent's text response to audio at the `/voice` endpoint.

**Configuration:**

```bash
VOICE_ENABLED=true
VOICE_TTS_VOICE_NAME=en-US-Journey-F
VOICE_TTS_AUDIO_ENCODING=OGG_OPUS
```

**Required:**
- Google Cloud Text-to-Speech API enabled
- `google-cloud-texttospeech` Python package: `pip install "emonk[voice]"`

See [Voice](voice.md) for the full guide.

---

## Sandbox Execution

### Modal.com

Optional sandboxed code execution backend. Runs Python code in isolated Modal containers.

**Install:**

```bash
pip install "emonk[modal]"
```

**Usage:**

```python
from emonk.sandbox import ModalSandboxBackend
from langchain_core.tools import tool

sandbox = ModalSandboxBackend()

@tool
async def run_code(code: str) -> str:
    """Execute Python code in an isolated sandbox."""
    result = await sandbox.execute(code)
    return result.output

agent = build_deep_agent(model=model, tools=[run_code], ...)
```

---

## Agent Orchestration

### LangChain / LangGraph

The foundation of monkey-bot. All agent state, tool calling, and memory flows through LangGraph.

**Required dependencies (installed automatically with emonk):**
- `langchain>=1.0.0`
- `langgraph>=1.0.0`
- `langchain-core>=1.0.0`
- `langchain-google-vertexai>=2.0.0`

---

### deepagents

Extended deep agent capabilities. monkey-bot's `build_deep_agent()` calls `deepagents.create_deep_agent()` under the hood.

**Install:**

```bash
pip install deepagents
```

Included automatically when you install `emonk`.
