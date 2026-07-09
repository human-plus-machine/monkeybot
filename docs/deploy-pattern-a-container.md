# Pattern A — Managed Container Deployment

Run monkeybot as a long-lived container process. The FastAPI SSE gateway handles sessions, streaming, and the agent loop. Storage and memory backends are selected via env vars. Sandbox (OpenSandbox) runs as a sidecar or as a separate service in the same private network.

**Targets covered:** GCP Cloud Run · GKE · GCE (VM) · AWS ECS · EKS · EC2 (VM) · Azure Container Apps · AKS · Azure VM · NVIDIA / other container hosts

---

## 1. Environment Variables

Set these in your platform's secret/env management. Values shown are the defaults baked into `monkeybot_config/monkeybot.yaml`; set them explicitly when deploying to override the baked-in config.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes (Gemini) | — | LLM provider key. Swap for `VERTEX_AI_PROJECT_ID` when using Vertex. |
| `DB_URL` | No | `sqlite:////app/data/monkeybot.db` | Storage backend. Use `postgresql://user:pass@host:5432/db` for managed Postgres or `firestore://PROJECT/(default)` for Firestore (`pip install 'monkeybot[firestore]'`). |
| `MEMORY_STORAGE_URI` | No | `local:///app/data/memory` | Workspace/memory backend. Use `gcs://bucket/prefix` or `s3://bucket/prefix` for cloud object storage. |
| `SANDBOX_ENABLED` | No | `false` | Set `true` to enable the OpenSandbox code-execution environment. |
| `SANDBOX_SERVER_URL` | If sandbox enabled | — | URL of the OpenSandbox server (e.g. `http://localhost:8080` for a sidecar, or a private IP for a VPC-separated service). |
| `SANDBOX_AUTH_TOKEN` | No | — | Bearer token forwarded to OpenSandbox as `Authorization: Bearer <token>`. Only needed when OpenSandbox is outside the private network. |

**Managed Postgres (SSL):** Cloud SQL, RDS, and Azure Database for PostgreSQL require TLS. Append the SSL parameter your provider documents to `DB_URL`:

```
# Cloud SQL / Azure
postgresql://user:pass@host:5432/db?sslmode=require

# RDS
postgresql://user:pass@host:5432/db?ssl=true&sslrootcert=/path/to/ca.pem
```

monkeybot passes the URL through to asyncpg unchanged after normalizing `postgres://` → `postgresql://`.

---

## 2. Build the Image

```bash
# Default — Gemini provider, SQLite + local FS (no cloud SDKs)
docker build -f docker/Dockerfile -t monkeybot:latest .

# Gemini + GCS memory
docker build -f docker/Dockerfile --build-arg EXTRAS=gemini,gcs -t monkeybot:latest .

# Gemini + Postgres storage
docker build -f docker/Dockerfile --build-arg EXTRAS=gemini,postgres -t monkeybot:latest .

# Vertex AI + GCS memory + Postgres storage (typical managed GCP stack)
docker build -f docker/Dockerfile --build-arg EXTRAS=vertex,gcs,postgres -t monkeybot:latest .

# Add sandbox support to any of the above
docker build -f docker/Dockerfile --build-arg EXTRAS=gemini,gcs,postgres,sandbox -t monkeybot:latest .
```

`EXTRAS` maps directly to pip install extras defined in `pyproject.toml`. Only install what you use — the base image has zero cloud-provider SDKs.

Push to your registry before deploying:

```bash
docker tag monkeybot:latest <registry>/<image>:<tag>
docker push <registry>/<image>:<tag>
```

---

## 3. Connect a Managed Postgres Database

1. Install the `[postgres]` extra at build time (`EXTRAS=...,postgres`).
2. Set `DB_URL` to a `postgresql://` connection string pointing at your managed instance.
3. By default monkeybot applies the schema on startup (`paths.auto_schema: true` in monkeybot.yaml). For DML-only runtime users, pre-create the schema via your migration tool and set `paths.auto_schema: false`.
4. The gateway opens the connection pool once at startup and closes it on shutdown.

**Short-lived processes (scale-to-zero):** On Cloud Run or ECS Fargate, a new instance starts a new connection pool. Postgres connection limits can be hit if many instances start simultaneously — consider a connection pooler (Cloud SQL Auth Proxy, RDS Proxy, PgBouncer) in front of the DB.

---

## 4. Connect Cloud Object Storage for Memory

Install the appropriate extra and set `MEMORY_STORAGE_URI`:

```bash
# GCS — requires [gcs] extra
MEMORY_STORAGE_URI=gcs://my-bucket/monkeybot-memory

# S3 — requires [aws] extra
MEMORY_STORAGE_URI=s3://my-bucket/monkeybot-memory
```

The factory (`create_workspace_storage`) reads the URI scheme and returns the right implementation. `append_text` operations under GCS/S3 are read-merge-write (not atomic) — the memory asyncio lock prevents races within a single process. Across multiple instances, avoid concurrent writes to the same memory key.

---

## 5. OpenSandbox Deployment

OpenSandbox always runs as a separate service. monkeybot connects to it via `SANDBOX_SERVER_URL`. Where OpenSandbox runs is an infrastructure decision, not a monkeybot config concern.

| Infrastructure | Where OpenSandbox runs | `SANDBOX_SERVER_URL` |
|---|---|---|
| Local Docker Compose | Sidecar (see `docker/docker-compose.sandbox.yml`) | `http://opensandbox-server:8080` |
| ECS (EC2 launch type) | Sidecar container in same task definition | `http://localhost:8080` |
| EKS / GKE | Sidecar container in same pod | `http://localhost:8080` |
| GCE / EC2 / Azure VM | Same host, sidecar container | `http://localhost:8080` |
| Cloud Run / ECS Fargate / Container Apps | Separate VM in same VPC | Private IP of the OpenSandbox VM |

**Authentication:** Network-layer isolation (VPC / private subnet) is the default and is sufficient for most deployments. If OpenSandbox must be reachable across a network boundary, set `SANDBOX_AUTH_TOKEN` in monkeybot's env — it is forwarded as `Authorization: Bearer <token>`. Configure OpenSandbox to require the token on its end.

**Note:** OpenSandbox requires access to the Docker daemon (`/var/run/docker.sock`). Platforms with no Docker socket (Cloud Run, ECS Fargate, Container Apps) cannot run OpenSandbox as a sidecar — deploy it on a separate VM/node in the same VPC.

---

## Per-Target Addenda

### GCP Cloud Run

**IAM roles for the service account:**

| Role | Why |
|---|---|
| `roles/secretmanager.secretAccessor` | Read secrets from Secret Manager |
| `roles/aiplatform.user` | Call Vertex AI models (if using Vertex provider) |
| `roles/cloudsql.client` | Connect to Cloud SQL via Auth Proxy (if using Cloud SQL) |
| `roles/storage.objectAdmin` | Read/write GCS memory bucket (if using GCS) |

**Managed DB:** Cloud SQL (Postgres). Use the Cloud SQL Auth Proxy or the direct connection string with `?sslmode=require`. The proxy runs as a sidecar if you're on GKE; on Cloud Run use the Cloud SQL connector via the connection name in `DB_URL`:

```
postgresql+asyncpg://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
```

**Secret management:** GCP Secret Manager. Mount secrets as env vars in the Cloud Run service definition.

**Build and deploy:**

```bash
# Set your project variables
PROJECT_ID=your-project
REGION=us-central1
SERVICE=monkeybot
IMAGE=gcr.io/${PROJECT_ID}/${SERVICE}
SA=${SERVICE}-sa@${PROJECT_ID}.iam.gserviceaccount.com

# Build with Cloud Build (no local Docker needed)
gcloud builds submit --tag ${IMAGE}:latest --project ${PROJECT_ID} \
  --build-arg EXTRAS=vertex,gcs,postgres .

# Create service account (first deploy only)
gcloud iam service-accounts create ${SERVICE}-sa \
  --display-name "monkeybot Service Account" \
  --project ${PROJECT_ID}

# Grant roles
for role in roles/secretmanager.secretAccessor roles/aiplatform.user roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SA}" --role="${role}" --condition=None
done

# Deploy
gcloud run deploy ${SERVICE} \
  --image ${IMAGE}:latest \
  --platform managed \
  --region ${REGION} \
  --project ${PROJECT_ID} \
  --service-account ${SA} \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --concurrency 1 \
  --min-instances 0 \
  --max-instances 3 \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest \
  --set-env-vars DB_URL=postgresql://...,MEMORY_STORAGE_URI=gcs://...
```

**Sandbox on Cloud Run:** Cloud Run has no Docker socket. Deploy OpenSandbox on a GCE VM or GKE node in the same VPC. Set `SANDBOX_SERVER_URL` to the private IP of that VM. Use `SANDBOX_AUTH_TOKEN` if the service is not fully isolated by firewall rules.

---

### GKE

**IAM / RBAC:** Use Workload Identity to bind the Kubernetes service account to a GCP service account. Grant the GCP service account the same roles as Cloud Run above.

```bash
gcloud iam service-accounts add-iam-policy-binding ${SA} \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA_NAME}]"
```

**Managed DB:** Cloud SQL via the Cloud SQL Auth Proxy sidecar in the pod, or direct connection with `sslmode=require`.

**Memory:** GCS bucket mounted via `MEMORY_STORAGE_URI=gcs://...` or a `PersistentVolumeClaim` with `MEMORY_STORAGE_URI=local:///mnt/memory`.

**Sandbox sidecar pod spec (excerpt):**

```yaml
containers:
  - name: monkeybot
    image: <registry>/monkeybot:latest
    env:
      - name: SANDBOX_ENABLED
        value: "true"
      - name: SANDBOX_SERVER_URL
        value: "http://localhost:8081"
  - name: opensandbox
    image: opensandbox/server:latest
    ports:
      - containerPort: 8081
    env:
      - name: OPENSANDBOX_INSECURE_SERVER
        value: "YES"
    volumeMounts:
      - name: docker-sock
        mountPath: /var/run/docker.sock
volumes:
  - name: docker-sock
    hostPath:
      path: /var/run/docker.sock
```

---

### GCE (VM)

**IAM:** Attach a service account to the VM instance with the same roles as Cloud Run. Use instance metadata for credentials — no key file needed.

**Install and run:**

```bash
# On the VM
docker pull <registry>/monkeybot:latest

docker run -d \
  --name monkeybot \
  -p 8080:8080 \
  -e GEMINI_API_KEY=... \
  -e DB_URL=postgresql://... \
  -e MEMORY_STORAGE_URI=gcs://... \
  <registry>/monkeybot:latest
```

**Sandbox sidecar:**

```bash
docker run -d \
  --name opensandbox \
  -p 8081:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e OPENSANDBOX_INSECURE_SERVER=YES \
  opensandbox/server:latest

docker run -d \
  --name monkeybot \
  -p 8080:8080 \
  --link opensandbox \
  -e SANDBOX_ENABLED=true \
  -e SANDBOX_SERVER_URL=http://opensandbox:8080 \
  ...
  <registry>/monkeybot:latest
```

**Secret management:** GCP Secret Manager accessed via the VM's service account, or Secret Manager API called at startup.

---

### AWS ECS

**IAM:** Create a task execution role and a task role. Attach the task role to the ECS task definition.

| Permission | Why |
|---|---|
| `secretsmanager:GetSecretValue` | Read secrets from Secrets Manager |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` | S3 memory bucket (if using S3) |
| `rds-db:connect` | RDS IAM auth (optional; alternatively use a DB password in Secrets Manager) |

**Managed DB:** RDS Postgres. Use RDS Proxy in front of RDS for scale-to-zero (Fargate) to avoid connection exhaustion. Set `DB_URL` from a Secrets Manager secret.

**Memory:** S3 bucket with `MEMORY_STORAGE_URI=s3://bucket/prefix`. Requires `[aws]` extra.

**Sandbox on ECS Fargate:** Not supported (no Docker socket). Deploy OpenSandbox on an EC2 instance in the same VPC. On EC2 launch type, add OpenSandbox as a sidecar container in the task definition and set `SANDBOX_SERVER_URL=http://localhost:8080`.

**Task definition excerpt:**

```json
{
  "family": "monkeybot",
  "taskRoleArn": "arn:aws:iam::ACCOUNT:role/monkeybot-task-role",
  "executionRoleArn": "arn:aws:iam::ACCOUNT:role/ecsTaskExecutionRole",
  "containerDefinitions": [
    {
      "name": "monkeybot",
      "image": "<registry>/monkeybot:latest",
      "portMappings": [{"containerPort": 8080}],
      "secrets": [
        {"name": "GEMINI_API_KEY", "valueFrom": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:gemini-api-key"},
        {"name": "DB_URL", "valueFrom": "arn:aws:secretsmanager:REGION:ACCOUNT:secret:monkeybot-db-url"}
      ],
      "environment": [
        {"name": "MEMORY_STORAGE_URI", "value": "s3://my-bucket/monkeybot-memory"}
      ]
    }
  ]
}
```

---

### EKS

Same container pattern as GKE. Use IRSA (IAM Roles for Service Accounts) instead of Workload Identity:

```bash
eksctl create iamserviceaccount \
  --name monkeybot-sa \
  --namespace default \
  --cluster my-cluster \
  --attach-policy-arn arn:aws:iam::ACCOUNT:policy/monkeybot-policy \
  --approve
```

**Managed DB:** RDS Postgres. **Memory:** S3 (`MEMORY_STORAGE_URI=s3://...`). Sandbox sidecar pod pattern is identical to GKE — see GKE addendum above, substituting `opensandbox` port as needed.

---

### EC2 (VM)

Same Docker run pattern as GCE. Use an EC2 instance profile (IAM role attached to the instance) instead of a service account key. Secrets via AWS Secrets Manager or Parameter Store, fetched at startup or injected by your configuration management tooling.

---

### Azure Container Apps / AKS / Azure VM

Same pattern as Cloud Run / GKE / GCE respectively. Key differences:

| Concern | Azure equivalent |
|---|---|
| Managed identity | System-assigned or user-assigned Managed Identity (replaces GCP/AWS service accounts) |
| Secret management | Azure Key Vault + Key Vault references in Container Apps |
| Managed DB | Azure Database for PostgreSQL — Flexible Server |
| Object storage memory | Azure Blob Storage — `[azure]` extra (planned; not yet released) |
| Container registry | Azure Container Registry |

**Container Apps — IAM:**

```bash
az containerapp identity assign --name monkeybot --resource-group my-rg --system-assigned
az keyvault set-policy --name my-vault --object-id <identity-object-id> --secret-permissions get list
```

**Sandbox on Container Apps:** No Docker socket available. Deploy OpenSandbox on an Azure VM in the same VNet. Set `SANDBOX_SERVER_URL` to the VM's private IP.

---

### NVIDIA DGX / NIM-Compatible Hosts and Other Container Hosts

Same pattern as GCE/EC2. Run the monkeybot container with the appropriate env vars. If the host has a Docker socket available, the OpenSandbox sidecar pattern works identically to GCE/EC2.

For GPU-accelerated LLM inference via NIM: point monkeybot at the NIM endpoint by configuring the provider in `monkeybot_config/monkeybot.yaml`; no changes to the container entrypoint or storage wiring are needed.
