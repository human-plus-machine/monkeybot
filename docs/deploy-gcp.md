# Deploy to GCP (Google Cloud Run)

This guide covers everything you need to deploy a monkey-bot agent to Google Cloud Run — from zero GCP setup to a live, production-grade bot with secrets management, persistent memory, distributed scheduling, and Google Chat integration.

---

## Architecture Overview

```
Internet / Google Chat
        │
        ▼
┌───────────────────────┐
│   Cloud Run Service   │  ← Your monkey-bot container
│   (monkey-bot)        │    Autoscales to 0 when idle
│   POST /webhook       │
│   POST /cron/tick     │
│   GET  /health        │
└──┬────────┬───────────┘
   │        │
   ▼        ▼
┌──────┐  ┌──────────────────┐     ┌───────────────────┐
│Vertex│  │ Cloud Scheduler  │     │  GCP Secret       │
│  AI  │  │ POST /cron/tick  │     │  Manager          │
│Gemini│  │ every minute     │     │  (secrets)        │
└──────┘  └──────────────────┘     └───────────────────┘
   │
   ▼
┌──────────────────┐   ┌──────────────────┐
│  Cloud Storage   │   │   Firestore      │
│  (GCS)          │   │  (checkpoints    │
│  Memory bucket  │   │   + job storage) │
└──────────────────┘   └──────────────────┘
```

---

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| `gcloud` CLI | GCP operations | [Install](https://cloud.google.com/sdk/docs/install) |
| GCP account with billing | Running services | [Console](https://console.cloud.google.com) |
| Docker (optional) | Local testing | [Install](https://docs.docker.com/get-docker/) |

---

## Part 1: GCP Infrastructure Setup

### 1.1 — Create or Select a GCP Project

```bash
# List existing projects
gcloud projects list

# Create a new project (if needed)
gcloud projects create my-monkey-bot --name="Monkey Bot"

# Set as your active project
gcloud config set project my-monkey-bot

# Enable billing (required for Cloud Run, Vertex AI)
# Do this in the console: https://console.cloud.google.com/billing
```

### 1.2 — Enable Required APIs

Run this once per project. It may take 2–3 minutes.

```bash
gcloud services enable \
    aiplatform.googleapis.com \
    run.googleapis.com \
    secretmanager.googleapis.com \
    cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com \
    firestore.googleapis.com \
    storage.googleapis.com \
    containerregistry.googleapis.com
```

Verify they're enabled:

```bash
gcloud services list --enabled --filter="name:(aiplatform OR run OR secretmanager OR cloudbuild OR cloudscheduler OR firestore OR storage)"
```

### 1.3 — Create the Service Account

The service account is the identity your Cloud Run service runs as.

```bash
PROJECT_ID=$(gcloud config get-value project)
SA_NAME="monkey-bot-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Create service account
gcloud iam service-accounts create ${SA_NAME} \
    --display-name="Monkey-Bot Service Account" \
    --project=${PROJECT_ID}
```

### 1.4 — Grant IAM Roles

```bash
# Vertex AI — call Gemini models
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user"

# Secret Manager — read secrets at runtime
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor"

# Cloud Run — invoke other services (optional, for sub-agent patterns)
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/run.invoker"

# Firestore — conversation checkpoints + scheduler job storage
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/datastore.user"

# Cloud Storage — GCS memory bucket
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectAdmin"
```

### 1.5 — Download Service Account Key (Local Dev Only)

```bash
gcloud iam service-accounts keys create ./service-account-key.json \
    --iam-account=${SA_EMAIL}
```

> **Security:** Add `service-account-key.json` to `.gitignore`. Never commit it. In production, Cloud Run uses the service account automatically — no key file needed.

---

## Part 2: Storage Setup

### 2.1 — Create GCS Memory Bucket

```bash
BUCKET_NAME="${PROJECT_ID}-monkey-bot-memory"

gcloud storage buckets create gs://${BUCKET_NAME} \
    --project=${PROJECT_ID} \
    --location=us-central1 \
    --uniform-bucket-level-access

# Grant service account access
gcloud storage buckets add-iam-policy-binding gs://${BUCKET_NAME} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectAdmin"
```

Update `bot.yaml`:

```yaml
memory:
  backend: gcs
  bucket: your-project-monkey-bot-memory
```

### 2.2 — Initialize Firestore

Firestore is used for conversation checkpoints and distributed scheduler job storage.

```bash
# Create Firestore database (native mode)
gcloud firestore databases create \
    --location=us-central1 \
    --project=${PROJECT_ID}
```

Update `bot.yaml`:

```yaml
scheduler:
  storage: firestore

# For conversation checkpoints, set this env var:
# CHECKPOINT_BACKEND=firestore
```

---

## Part 3: Secrets Management

Store all sensitive values in GCP Secret Manager. monkey-bot loads them automatically at startup when `secrets.provider: gcp_secret_manager` is set in `bot.yaml`.

### 3.1 — Create Required Secrets

```bash
PROJECT_ID=$(gcloud config get-value project)

# GCP project ID for Vertex AI
echo -n "${PROJECT_ID}" | gcloud secrets create vertex-ai-project-id \
    --data-file=- \
    --replication-policy="automatic" \
    --project=${PROJECT_ID}

# Comma-separated list of allowed user emails
echo -n "alice@company.com,bob@company.com" | gcloud secrets create allowed-users \
    --data-file=- \
    --replication-policy="automatic" \
    --project=${PROJECT_ID}
```

### 3.2 — Create Optional Secrets

```bash
# Google Chat webhook URL (for posting scheduled job results)
echo -n "https://chat.googleapis.com/v1/spaces/.../messages?key=..." | \
    gcloud secrets create google-chat-webhook \
    --data-file=- \
    --replication-policy="automatic" \
    --project=${PROJECT_ID}

# Cron endpoint secret (prevents unauthorized /cron/tick calls)
openssl rand -hex 32 | gcloud secrets create cron-secret \
    --data-file=- \
    --replication-policy="automatic" \
    --project=${PROJECT_ID}
```

### 3.3 — Verify Secrets

```bash
# List all secrets
gcloud secrets list --project=${PROJECT_ID}

# Verify a secret's value
gcloud secrets versions access latest --secret="vertex-ai-project-id" --project=${PROJECT_ID}
```

### 3.4 — Update bot.yaml for Production

```yaml
secrets:
  provider: gcp_secret_manager

gcp:
  project_id: your-project-id
  location: us-central1
```

---

## Part 4: Deploy to Cloud Run

### Option A — Automated Deploy Script (Recommended)

The `test-monkey` reference bot includes a `deploy.sh` that handles the full build and deploy cycle.

```bash
cd test-monkey
chmod +x deploy.sh
./deploy.sh
```

The script:
1. Creates/verifies the service account and IAM roles
2. Builds your Docker image with Cloud Build
3. Pushes to Google Container Registry
4. Deploys to Cloud Run with secrets wired in

### Option B — Manual Deploy with gcloud

```bash
PROJECT_ID=$(gcloud config get-value project)
REGION=us-central1
SERVICE_NAME=monkey-bot
SA_EMAIL="monkey-bot-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud run deploy ${SERVICE_NAME} \
    --source . \
    --platform managed \
    --region ${REGION} \
    --project ${PROJECT_ID} \
    --service-account ${SA_EMAIL} \
    --allow-unauthenticated \
    --memory 512Mi \
    --cpu 1 \
    --timeout 300 \
    --concurrency 1 \
    --max-instances 10 \
    --min-instances 0 \
    --set-env-vars "ENVIRONMENT=production,CHECKPOINT_BACKEND=firestore" \
    --set-secrets "VERTEX_AI_PROJECT_ID=vertex-ai-project-id:latest,ALLOWED_USERS=allowed-users:latest,GOOGLE_CHAT_WEBHOOK=google-chat-webhook:latest"
```

**Resource sizing guidance:**

| Workload | Memory | CPU | Max Instances |
|---|---|---|---|
| Light (single user) | 512Mi | 1 | 1 |
| Medium (small team) | 1Gi | 2 | 3 |
| Heavy (large team) | 2Gi | 4 | 10 |

### Option C — Build and Deploy Separately

```bash
PROJECT_ID=$(gcloud config get-value project)
IMAGE="gcr.io/${PROJECT_ID}/monkey-bot:latest"

# Build image with Cloud Build
gcloud builds submit --tag ${IMAGE} --project ${PROJECT_ID}

# Deploy the pre-built image
gcloud run deploy monkey-bot \
    --image ${IMAGE} \
    --region us-central1 \
    --project ${PROJECT_ID} \
    --service-account "monkey-bot-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --allow-unauthenticated
```

---

## Part 5: Verify Deployment

### 5.1 — Get Service URL

```bash
SERVICE_URL=$(gcloud run services describe monkey-bot \
    --region us-central1 \
    --project ${PROJECT_ID} \
    --format='value(status.url)')

echo "Service URL: ${SERVICE_URL}"
```

### 5.2 — Health Check

```bash
curl ${SERVICE_URL}/health
```

Expected response:

```json
{"status": "healthy", "agent": "monkey-bot"}
```

### 5.3 — Check Logs

```bash
# Recent logs
gcloud run services logs read monkey-bot \
    --region us-central1 \
    --project ${PROJECT_ID} \
    --limit 50

# Follow logs in real time
gcloud run services logs tail monkey-bot \
    --region us-central1 \
    --project ${PROJECT_ID}
```

Look for these startup messages to confirm everything initialized:

```
INFO: monkey-bot agent initialized
INFO: Auto-added schedule_task tool
INFO: Auto-added search_memory tool
INFO: Loaded skills: diagnostics
INFO: GCS filesystem sync started
INFO: Firestore checkpointer initialized
INFO: Scheduler started (storage: firestore)
INFO: Uvicorn running on http://0.0.0.0:8080
```

### 5.4 — Send a Test Message

```bash
curl -X POST ${SERVICE_URL}/run \
    -H "Content-Type: application/json" \
    -d '{"message": "hello", "user_id": "test"}'
```

---

## Part 6: Configure Google Chat

### 6.1 — Create a Google Chat App

1. Go to [Google Cloud Console → APIs & Services → Google Chat API](https://console.cloud.google.com/apis/api/chat.googleapis.com)
2. Click **Enable** if not already enabled
3. Click **Configuration** tab
4. Set **App name**: your bot name
5. Set **HTTP Endpoint URL**: `${SERVICE_URL}/webhook`
6. Set **Connection settings**: HTTP endpoint
7. Under **Slash commands**, optionally add commands (e.g., `/run`, `/schedule`)
8. Click **Save**

### 6.2 — Get a Webhook URL (for Outbound Notifications)

Your bot needs a webhook URL to post scheduled job results back to a space.

1. Open [Google Chat](https://chat.google.com)
2. Open (or create) the space where your bot will post
3. Click the space name → **Apps & integrations** → **Manage webhooks**
4. Click **Add webhook**
5. Name: "monkey-bot"
6. Click **Save** and copy the URL

Store it in Secret Manager:

```bash
echo -n "https://chat.googleapis.com/v1/spaces/.../messages?key=..." | \
    gcloud secrets create google-chat-webhook \
    --data-file=- \
    --project=${PROJECT_ID}
```

Then update your Cloud Run service to include it:

```bash
gcloud run services update monkey-bot \
    --region us-central1 \
    --update-secrets "GOOGLE_CHAT_WEBHOOK=google-chat-webhook:latest"
```

### 6.3 — Add Bot to a Space

1. Open your Google Chat space
2. Click the **+** icon → **Add apps**
3. Search for your app name
4. Click **Add**
5. The bot will post a welcome message when added

### 6.4 — Test the Bot

In the Google Chat space:

```
@your-bot Hello, are you there?
```

The bot should respond within 2–5 seconds.

---

## Part 7: Set Up Cloud Scheduler

Cloud Scheduler calls `/cron/tick` every minute, which lets the bot execute scheduled jobs.

### 7.1 — Create the Scheduler Service Account

```bash
TICK_SA_NAME="monkey-bot-tick"
TICK_SA_EMAIL="${TICK_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create ${TICK_SA_NAME} \
    --display-name="Monkey-Bot Cron Trigger" \
    --project=${PROJECT_ID}

# Allow it to invoke the Cloud Run service
gcloud run services add-iam-policy-binding monkey-bot \
    --region us-central1 \
    --member="serviceAccount:${TICK_SA_EMAIL}" \
    --role="roles/run.invoker" \
    --project=${PROJECT_ID}
```

### 7.2 — Create the Scheduler Job

```bash
gcloud scheduler jobs create http monkey-bot-tick \
    --location us-central1 \
    --schedule "* * * * *" \
    --uri "${SERVICE_URL}/cron/tick" \
    --http-method POST \
    --oidc-service-account-email "${TICK_SA_EMAIL}" \
    --oidc-token-audience "${SERVICE_URL}" \
    --time-zone "America/New_York" \
    --project ${PROJECT_ID}
```

### 7.3 — Verify the Scheduler

```bash
# Describe the job
gcloud scheduler jobs describe monkey-bot-tick \
    --location us-central1 \
    --project ${PROJECT_ID}

# Manually trigger it
gcloud scheduler jobs run monkey-bot-tick \
    --location us-central1 \
    --project ${PROJECT_ID}

# Check the logs
gcloud run services logs read monkey-bot \
    --region us-central1 \
    --limit 10 | grep -i scheduler
```

Look for:
```
INFO: Scheduler tick completed: 0 checked, 0 due, 0 executed
```

### 7.4 — Test Scheduling via Google Chat

In Google Chat:

```
@your-bot schedule a diagnostic report for 2 minutes from now
```

The agent calls `schedule_task`, stores the job in Firestore, and when Cloud Scheduler triggers `/cron/tick` at the right time, the job executes.

---

## Part 8: Production Hardening

### Enable Min Instances (Eliminate Cold Starts)

```bash
gcloud run services update monkey-bot \
    --region us-central1 \
    --min-instances 1 \
    --project ${PROJECT_ID}
```

> Note: Min instances = 1 means the service is always warm. This adds ~$20–50/month depending on resource allocation.

### Configure Autoscaling

```bash
gcloud run services update monkey-bot \
    --region us-central1 \
    --min-instances 1 \
    --max-instances 5 \
    --concurrency 10 \
    --project ${PROJECT_ID}
```

### Set Up Cloud Monitoring Alerts

```bash
# Alert on error rate > 5%
gcloud alpha monitoring policies create \
    --notification-channels="your-channel-id" \
    --display-name="monkey-bot error rate" \
    --condition-filter='resource.type="cloud_run_revision" AND metric.type="run.googleapis.com/request_count" AND metric.labels.response_code_class="5xx"'
```

### Enable VPC (if needed for private access)

```bash
gcloud run services update monkey-bot \
    --region us-central1 \
    --vpc-connector your-vpc-connector \
    --vpc-egress all-traffic \
    --project ${PROJECT_ID}
```

---

## Useful Commands Reference

```bash
# View service status
gcloud run services describe monkey-bot --region us-central1

# Update env vars
gcloud run services update monkey-bot \
    --region us-central1 \
    --set-env-vars "KEY=value"

# Update secrets
gcloud run services update monkey-bot \
    --region us-central1 \
    --update-secrets "SECRET_NAME=secret-name:latest"

# Roll back to previous revision
gcloud run services update-traffic monkey-bot \
    --region us-central1 \
    --to-revisions PREV=100

# Delete service
gcloud run services delete monkey-bot --region us-central1

# List all revisions
gcloud run revisions list --service monkey-bot --region us-central1

# Trigger Cloud Scheduler manually
gcloud scheduler jobs run monkey-bot-tick --location us-central1

# Check Firestore data (for debugging scheduler)
gcloud firestore documents list projects/${PROJECT_ID}/databases/(default)/documents/scheduler_jobs
```

---

## Troubleshooting

### "Failed to load secrets"

```
RuntimeError: Failed to load required secrets: vertex-ai-project-id
```

**Fix:**

```bash
# Check the secret exists
gcloud secrets list --project=${PROJECT_ID}

# Check service account has access
gcloud projects get-iam-policy ${PROJECT_ID} \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:monkey-bot-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# If missing secretmanager.secretAccessor, add it:
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:monkey-bot-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
```

### Service fails to start (503)

```bash
# Get startup logs
gcloud run services logs read monkey-bot --region us-central1 --limit 100
```

Common causes:
- Missing required env var — look for `KeyError` or `ValueError` in logs
- Import error — look for `ModuleNotFoundError`
- Secret not found — look for `google.api_core.exceptions.NotFound`

### "The app didn't respond" in Google Chat

The Cloud Run service returned a non-200 status code or timed out.

```bash
# Check logs for the failed request
gcloud run services logs read monkey-bot \
    --region us-central1 \
    --filter="httpRequest.status>=400" \
    --limit 20
```

Common causes:
- Vertex AI quota exceeded — check [quotas page](https://console.cloud.google.com/iam-admin/quotas)
- Timeout — Google Chat has a 30-second response timeout. Increase `--timeout` on Cloud Run and optimize agent response time
- `allowed_users` doesn't include the user's email

### Scheduler jobs not executing

```bash
# 1. Verify Cloud Scheduler is hitting the endpoint
gcloud scheduler jobs describe monkey-bot-tick --location us-central1

# 2. Manually trigger and check response
gcloud scheduler jobs run monkey-bot-tick --location us-central1
curl -X POST ${SERVICE_URL}/cron/tick  # Should return 200

# 3. Check the jobs storage (Firestore)
gcloud firestore documents list \
    "projects/${PROJECT_ID}/databases/(default)/documents/scheduler_jobs"

# 4. Check for datetime errors in logs
gcloud run services logs read monkey-bot --region us-central1 | grep -i "scheduler\|cron\|tick"
```

---

## Cost Estimate

Approximate monthly costs for a small production deployment:

| Service | Tier | Est. Monthly Cost |
|---|---|---|
| Cloud Run (1 min instance) | 0.5 vCPU / 512Mi | ~$15–25 |
| Vertex AI (Gemini 2.5 Flash) | ~500K tokens/mo | ~$5–15 |
| Cloud Storage | 1 GB | < $1 |
| Firestore | Light usage | < $1 |
| Cloud Scheduler | 1 job | Free (first 3 jobs) |
| Secret Manager | 5 secrets | < $1 |
| Cloud Build | ~10 builds/mo | Free tier |

**Total: ~$20–45/month** for a small team bot.

---

## Next Steps

- Set up [Google Chat integration](integrations.md#google-chat)
- Configure the [Memory System](memory.md) for GCS persistence
- Set up [background jobs](scheduler.md)
- Review the [Configuration Reference](configuration.md) for all options
