# Deploy to AWS

> **This guide is coming soon.**
>
> AWS deployment support is actively in development. This page outlines the planned architecture and what will be covered.

---

## Planned Architecture

When AWS support ships, monkey-bot will deploy to **AWS ECS on Fargate** — the closest equivalent to GCP Cloud Run. The full stack will look like:

```
Internet / Slack / Teams / API
        │
        ▼
┌───────────────────────┐
│   ECS Fargate Task    │  ← Your monkey-bot container
│   (monkey-bot)        │    Auto-scales, serverless
│   POST /webhook       │
│   POST /cron/tick     │
│   GET  /health        │
└──┬────────┬───────────┘
   │        │
   ▼        ▼
┌──────┐  ┌──────────────────┐     ┌───────────────────┐
│Amazon│  │ EventBridge      │     │  AWS Secrets      │
│Bedrock  │ Scheduler        │     │  Manager          │
│(Bedrock │ (cron trigger)   │     │  (secrets)        │
│Claude)  └──────────────────┘     └───────────────────┘
└──────┘
   │
   ▼
┌──────────────────┐   ┌──────────────────┐
│     S3           │   │   DynamoDB       │
│  (memory bucket) │   │  (checkpoints   │
│                  │   │   + job storage) │
└──────────────────┘   └──────────────────┘
```

---

## What Will Be Covered

### Infrastructure Setup
- AWS account and IAM configuration
- ECS cluster and Fargate service creation
- ECR (Elastic Container Registry) for Docker images
- Application Load Balancer setup
- VPC and security group configuration
- CDK (Cloud Development Kit) infrastructure templates

### Secrets & Config
- AWS Secrets Manager integration
- Parameter Store for non-secret config
- Environment variable injection into ECS tasks
- `secrets.provider: aws_secrets_manager` configuration

### Storage Backends
- S3 memory bucket setup and IAM policies
- `memory.backend: s3` configuration
- DynamoDB table for scheduler job storage
- `scheduler.storage: dynamodb` configuration

### LLM Providers
- Amazon Bedrock setup (Claude 3, Llama 3, Titan)
- `model.provider: aws_bedrock` configuration
- Bedrock IAM permissions

### Scheduling
- AWS EventBridge Scheduler (replacement for Cloud Scheduler)
- HTTPS target configuration for `/cron/tick`
- IAM role for EventBridge to invoke ECS

### Deployment
- `deploy-aws.sh` — one-command deploy script
- CDK app for full infrastructure as code
- GitHub Actions CI/CD pipeline template

---

## Current Status

| Component | Status |
|---|---|
| ECS Fargate deployment | In Development |
| S3 memory backend | In Development |
| DynamoDB scheduler storage | In Development |
| AWS Secrets Manager | In Development |
| Amazon Bedrock LLM provider | In Development |
| EventBridge cron trigger | In Development |
| CDK infrastructure templates | Planned |
| One-command deploy script | Planned |

---

## Get Notified

Watch this repository for updates: [GitHub](https://github.com/human-and-machine/monkey-bot)

In the meantime, you can deploy monkey-bot on AWS today using the GCP-native features with your own AWS infrastructure, or use the [GCP deployment guide](deploy-gcp.md) while we build out AWS support.

---

## Interim: Run on AWS with GCP Backend

If you need to run monkey-bot on AWS infrastructure now (e.g., in a VPC alongside other AWS services), you can use AWS ECS with GCP credentials for Vertex AI and GCS:

```yaml
# bot.yaml — Run on ECS, use GCP for LLM + storage
model:
  provider: google_vertexai
  name: gemini-2.5-flash

memory:
  backend: gcs
  bucket: my-gcs-bucket

gcp:
  project_id: my-gcp-project
```

Mount your GCP service account key as an ECS secret and set `GOOGLE_APPLICATION_CREDENTIALS`. This is a valid cross-cloud architecture while native AWS support is in development.

---

## Feedback

Have specific AWS infrastructure requirements? Open an issue or discussion on GitHub so we can prioritize the right components.

[Open a GitHub Issue](https://github.com/human-and-machine/monkey-bot/issues/new?labels=aws&title=AWS+deployment+feedback)
