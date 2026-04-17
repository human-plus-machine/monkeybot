# AWS enterprise runbook (≤ 30 minutes)

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Topology aligns with the shipped AWS enterprise example under `examples/aws-enterprise-agent/`.

The AWS enterprise stack is the primary deliverable of the
`harness-extensibility` feature: a consumer stands up a production-grade
agent on AWS in under 30 minutes starting from an empty account.

The companion example repo layout lives at
[`examples/aws-enterprise-agent/`](../../examples/aws-enterprise-agent/) and
ships the Dockerfile, `harness.yaml`, IAM policy, and a Terraform module
that stamps out RDS + S3 + IAM + Bedrock AgentCore. The links below remain
stable even while that example evolves.

> **Quickstart**: follow the 5-step README in
> [`examples/aws-enterprise-agent/README.md`](../../examples/aws-enterprise-agent/README.md).
> It drives Terraform → ECR → ECS/AgentCore → `GET /harness/aws/smoke`
> end-to-end and is the canonical "30-minute" path.

## Cohesive stack

```
PostgresCheckpointer         // session durability (RDS Postgres)
S3MemoryStore                // long-term memory (S3, server-side encrypted)
PostgresJobStorage           // scheduler leases (same RDS cluster)
S3IdentitySource             // SOUL/RULES/IDENTITY files (S3)
AWSSecretsManagerResolver    // secrets (Secrets Manager)
BedrockProvider              // model (Bedrock Converse)
S3RunPackageWriter           // RunPackage artifacts (S3)
```

A single RDS cluster backs both Postgres surfaces — the shared-pool
implementation means one DSN registration consumes one pool, not three
(see [`postgres-backends.md`](postgres-backends.md)).

## IAM policy (attach to AgentCore task role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-*"
    },
    {
      "Sid": "MemoryBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::emonk-memory-prod/*"
    },
    {
      "Sid": "IdentityBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::emonk-identity/prod/*"
    },
    {
      "Sid": "RunPackageBucket",
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::emonk-runs-prod/*"
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:*:secret:ckpt-dsn-*",
        "arn:aws:secretsmanager:us-east-1:*:secret:app-*"
      ]
    }
  ]
}
```

## `harness.yaml`

```yaml
agent:
  name: enterprise-agent
  model: anthropic.claude-3-5-sonnet-20241022-v2:0
  provider: bedrock

checkpointer:
  backend: postgres
  kwargs:
    dsn_env: CKPT_DSN
    schema_name: emonk_ckpt

memory_store:
  backend: s3
  kwargs:
    bucket: emonk-memory-prod
    prefix: prod/

job_storage:
  backend: postgres
  kwargs:
    dsn_env: CKPT_DSN
    schema_name: emonk_scheduler

identity_source:
  backend: s3
  kwargs:
    bucket: emonk-identity
    prefix: prod/

secret_resolver:
  backend: aws_secrets_manager
  kwargs:
    region: us-east-1

model_provider:
  backend: bedrock
  kwargs:
    region: us-east-1
```

## Env vars

```bash
HARNESS_CONFIG=/app/harness.yaml
HARNESS_PLUGINS_FROM_ENTRY_POINTS=0      # lock down in prod
CKPT_DSN=<secret-handle resolved via AWS SM>
AWS_REGION=us-east-1
```

## Deployment checklist

- [ ] **RDS** Postgres provisioned (`db.m6g.large` for ≤ 10 concurrent
  sessions; see the sizing table in
  [`postgres-backends.md`](postgres-backends.md)). `max_connections=200`.
- [ ] **S3 buckets** `emonk-memory-prod`, `emonk-identity`, `emonk-runs-prod`
  created with server-side encryption enabled.
- [ ] **Secrets Manager** secrets `ckpt-dsn`, `app-openai-key`, … created
  with rotation rules matching your compliance posture.
- [ ] **IAM role** attached to the AgentCore task, policy above applied.
- [ ] **VPC endpoints** for `s3`, `secretsmanager`, `bedrock-runtime` + an
  RDS subnet association (keeps traffic off the public internet).
- [ ] **Image built** from
  [`Dockerfile.aws-enterprise`](../../examples/aws-enterprise-agent/Dockerfile.aws-enterprise)
  (or the generic
  [`Dockerfile.extension-template`](../../Dockerfile.extension-template))
  with a hash-pinned `requirements.lock.txt`.
- [ ] **Smoke probe** — `GET /harness/aws/smoke` (admin-token + feature-flag
  gated — `HARNESS_ENABLE_AWS_SMOKE=1`) returns
  `{"data": {"checks": [...], "all_pass": true, "ok": true, "probes": [...]}}`
  with every check `status: "pass"` (Postgres, S3, Secrets Manager, Bedrock,
  KMS, STS). Any failure yields **HTTP 503** with `code: HARNESS_AWS_SMOKE_FAIL`
  while still returning the `checks` array for triage.

## Break-glass

If a backend misbehaves post-deploy:

1. Set `HARNESS_CHECKPOINTER_OVERRIDE=in_memory` (or the relevant surface
   override) to fall back to the InMemory reference.
2. Restart the container — in-flight sessions lose durability until
   restored.
3. Diagnose with `plugin ls --strict` + `GET /harness/health` + the
   `EV_HEALTH_DEGRADED` events in your observability backend.
4. Revert the override and redeploy once the real backend is healthy.

## Runnable snippet

```bash
# Verify an AWS deploy after the image is running.
# - HARNESS_ENABLE_AWS_SMOKE=1 must be set on the container.
# - X-Admin-Token must match EMONK_ADMIN_TOKEN.
curl -fsSL https://agent.example.com/harness/aws/smoke \
  -H "X-Admin-Token: ${EMONK_ADMIN_TOKEN}" | jq '.data'
# Expected response shape (canonical fields):
#   { "all_pass": true,
#     "checks": [
#       { "name": "postgres.ckpt.ping", "status": "pass", "latency_ms": 12,
#         "probe": "postgres.ckpt.ping", "reachable": true },
#       ...
#     ],
#     "ok": true,
#     "probes": [ ... same rows ... ]
#   }
# Any "status": "fail" (or "reachable": false) sets all_pass=false; HTTP 503
# with code HARNESS_AWS_SMOKE_FAIL — read "error_class" on failed rows (no raw
# exception messages are returned).
```
