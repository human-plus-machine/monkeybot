# Getting Started

Get a monkey-bot agent running locally in under 5 minutes, then deploy to production.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ | Required |
| pip | Latest | Package management |
| Google Cloud account | — | For Vertex AI (Gemini) |
| gcloud CLI | Latest | For GCP operations |

Install gcloud if you haven't: https://cloud.google.com/sdk/docs/install

---

## Option A — Clone the Reference Bot

The fastest path. `test-monkey` is a complete, working bot that shows every framework feature.

```bash
# Clone the repo
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot/test-monkey

# Create your secrets file
cp .env.example .env
```

Edit `.env`:

```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/service-account.json
VERTEX_AI_PROJECT_ID=your-gcp-project-id
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run:

```bash
python -m src.main
```

You should see:

```
INFO:     monkey-bot agent initialized
INFO:     Auto-added schedule_task tool
INFO:     Loaded skills: diagnostics
INFO:     Uvicorn running on http://0.0.0.0:8080
```

Your agent is running. Test it:

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"message": "run diagnostics", "user_id": "test"}'
```

---

## Option B — Start from Scratch

Use this when you're building a new bot and want full control over the project structure.

### 1. Install emonk

```bash
pip install emonk
# or with GCS memory support:
pip install "emonk[gcs]"
```

### 2. Create project structure

```bash
mkdir my-bot && cd my-bot
mkdir -p src skills data/memory config tests
touch bot.yaml .env src/main.py src/job_handlers.py
```

### 3. Create bot.yaml

This file contains all non-secret configuration. It ships with your code and is baked into your Docker image.

```yaml
# bot.yaml
agent:
  name: my-bot
  skills_dir: ./skills

model:
  provider: google_vertexai
  name: gemini-2.5-flash
  temperature: 0.7
  max_tokens: 8192

server:
  port: 8080
  log_level: INFO

gateway:
  allowed_users:
    - you@yourcompany.com
  chat_format: workspace_addon

memory:
  dir: ./data/memory
  backend: local              # Use 'gcs' in production

scheduler:
  storage: json               # Use 'firestore' in production
  cadence: "* * * * *"
  timezone: America/New_York

secrets:
  provider: env               # Use 'gcp_secret_manager' in production

gcp:
  project_id: your-gcp-project
  location: us-central1
```

### 4. Create .env

```bash
# .env (never commit this file)
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
VERTEX_AI_PROJECT_ID=your-gcp-project-id

# Optional — for posting scheduled results to Google Chat
# GOOGLE_CHAT_WEBHOOK=https://chat.googleapis.com/v1/spaces/.../messages
```

### 5. Write your agent

```python
# src/main.py
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from emonk.core.config import load_secrets, get_model, get_system_prompt
from emonk.core.deepagent import build_deep_agent
from emonk.core.scheduler import CronScheduler, create_storage
from langchain_core.tools import tool

from src.job_handlers import register_handlers

# Load config and secrets at startup
load_secrets()

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # Replace with real weather API call
    return f"It's sunny and 72°F in {city}."

def create_agent():
    model = get_model()
    scheduler = CronScheduler(storage=create_storage())
    
    agent = build_deep_agent(
        model=model,
        tools=[get_weather],
        user_system_prompt="""You are a helpful assistant with weather capabilities.
        When asked about the weather, use the get_weather tool.""",
        scheduler=scheduler,
    )
    agent.scheduler = scheduler
    register_handlers(scheduler)
    return agent, scheduler

agent, scheduler = create_agent()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await scheduler.start()
    yield
    await scheduler.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/run")
async def run(request: Request):
    body = await request.json()
    message = body.get("message", "")
    user_id = body.get("user_id", "anonymous")
    
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": message}]},
        config={"configurable": {"thread_id": user_id}},
    )
    return {"response": result["messages"][-1].content}

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8080, reload=True)
```

### 6. Run it

```bash
python -m src.main
```

### 7. Test it

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"message": "what is the weather in New York?", "user_id": "alice"}'
```

---

## Setting Up GCP Credentials (Local Dev)

Monkey-bot uses Google Vertex AI for Gemini models by default. You need a service account.

### 1. Create a service account

```bash
PROJECT_ID=your-gcp-project-id

gcloud iam service-accounts create monkey-bot-dev \
    --display-name="Monkey-Bot Dev" \
    --project=${PROJECT_ID}
```

### 2. Grant Vertex AI access

```bash
SA_EMAIL="monkey-bot-dev@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user"
```

### 3. Download the key

```bash
gcloud iam service-accounts keys create ./service-account-key.json \
    --iam-account=${SA_EMAIL}
```

### 4. Set the env var

```bash
# In .env:
GOOGLE_APPLICATION_CREDENTIALS=./service-account-key.json
```

> **Never commit `service-account-key.json` to git.** Add it to `.gitignore`.

---

## Verify Everything Works

```bash
# 1. Check the health endpoint
curl http://localhost:8080/health
# → {"status":"healthy"}

# 2. Send a message
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"message": "hello", "user_id": "test"}'
# → {"response":"Hello! How can I help you today?"}

# 3. Run tests (if starting from test-monkey)
python -m pytest tests/ -v
# → 6 passed
```

---

## Next Steps

| Goal | Guide |
|---|---|
| Add tools and skills to your agent | [Creating an Agent](creating-an-agent.md) |
| Deploy to Google Cloud Run | [Deploy to GCP](deploy-gcp.md) |
| Connect to Google Chat | [Deploy to GCP → Google Chat Setup](deploy-gcp.md#configure-google-chat) |
| Set up background jobs | [Scheduler & Jobs](scheduler.md) |
| Understand all config options | [Configuration Reference](configuration.md) |
| Add voice capabilities | [Voice](voice.md) |
