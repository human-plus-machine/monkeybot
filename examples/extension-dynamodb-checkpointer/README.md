# `emonk-ext-dynamodb-ckpt` — DynamoDB Checkpointer for monkey-bot

A ~100-line, pip-installable extension that implements the monkey-bot
`Checkpointer` ABC against **Amazon DynamoDB**. It is the canonical worked
example for the
[Harness Extensibility guide](../../docs/extending-the-harness.md) and
demonstrates shipping a new backend with zero framework modifications.

> **Why DynamoDB is an extension (not a shipped backend).** monkey-bot ships
> `PostgresCheckpointer` for AWS enterprises because it covers the majority of
> session-durability needs with one operational pattern. DynamoDB is a great
> fit for fully-serverless AWS deployments but adds an AWS-only code path the
> framework deliberately keeps off the core dependency graph
> (see [`docs/harness/backend-matrix.md`](../../docs/harness/backend-matrix.md)).

---

## 10-minute quickstart

### 1. Create the DynamoDB table

The extension expects an already-provisioned table. Use Terraform, CDK, or the
AWS CLI — the schema is tiny:

```hcl
# terraform/main.tf
resource "aws_dynamodb_table" "emonk_checkpoints" {
  name         = "emonk-checkpoints"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "checkpoint_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "checkpoint_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}
```

Or via the CLI:

```bash
aws dynamodb create-table \
  --table-name emonk-checkpoints \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
    AttributeName=session_id,AttributeType=S \
    AttributeName=checkpoint_id,AttributeType=S \
  --key-schema \
    AttributeName=session_id,KeyType=HASH \
    AttributeName=checkpoint_id,KeyType=RANGE
```

### 2. Install the extension

```bash
pip install -e examples/extension-dynamodb-checkpointer
# or, once published to PyPI / a private index:
pip install emonk-ext-dynamodb-ckpt
```

### 3. Wire it into your `HarnessConfig`

Three mechanisms, any of which works:

**A. Programmatic** (no env var required):

```python
from emonk.core.harness.extensions import Checkpointer
from dynamodb_ckpt import DynamoDBCheckpointer

Checkpointer.registry.register(
    "dynamodb",
    lambda: DynamoDBCheckpointer(table_name="emonk-checkpoints", region="us-east-1"),
)
```

**B. YAML `import_path:`** (no startup-time code):

```yaml
# harness.yaml
checkpointer:
  import_path: "dynamodb_ckpt.checkpointer:DynamoDBCheckpointer"
  kwargs:
    table_name: emonk-checkpoints
    region: us-east-1
```

**C. Entry points** (supply-chain–gated):

```bash
export HARNESS_PLUGINS_FROM_ENTRY_POINTS=1
```

```yaml
# harness.yaml
checkpointer:
  backend: dynamodb
  kwargs:
    table_name: emonk-checkpoints
    region: us-east-1
```

The `emonk.checkpointers` entry point is declared in this package's
[`pyproject.toml`](pyproject.toml) — no framework patch required.

### 4. Attach IAM

Minimum policy for the Bedrock AgentCore / ECS task role:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:BatchWriteItem"
    ],
    "Resource": "arn:aws:dynamodb:us-east-1:*:table/emonk-checkpoints"
  }]
}
```

### 5. Deploy

Use the supplied [`Dockerfile`](Dockerfile) — it is a 1:1 copy of the
[`Dockerfile.extension-template`](../../Dockerfile.extension-template) with
`aioboto3` + this extension pre-installed. Point your build at it and ship.

---

## Run the contract test

The extension ships with one test — `tests/test_dynamodb_ckpt_contract.py` —
that runs the framework's **CKPT-C-01 … CKPT-C-07** invariants against a
`moto`-backed DynamoDB. The whole file is a single
`checkpointer_contract_suite(factory)` call plus a fixture that stands up the
table. This proves the same contract used to validate shipped backends can be
driven from consumer code with no framework imports under `tests/`.

```bash
pip install -e "examples/extension-dynamodb-checkpointer[dev]"
pytest examples/extension-dynamodb-checkpointer/tests -x -q
```

The `[dev]` extra pulls in `moto[server]` because the suite drives DynamoDB
through an in-process `ThreadedMotoServer` (not `moto.mock_aws()`): the latter
monkey-patches `botocore`'s synchronous HTTP transport, which is incompatible
with the `aiobotocore` transport `aioboto3` uses for async calls. The server
variant speaks real HTTP over loopback, matching the code path a consumer
would exercise against DynamoDB Local or LocalStack.

If `moto`, `moto.server`, or `aioboto3` are not installed the suite skips
cleanly rather than fail — keeping the main regression matrix green.

> **CKPT-C-07 note.** DynamoDB's per-item hard limit is 400 KB. CKPT-C-07
> writes a 1 MB payload, so the test intentionally reports it as skipped
> (via the contract's `ContractSkipped` escape hatch) rather than as a
> failure. Large payloads should be offloaded through `ContextPolicyMW`
> before checkpointing — see the operational notes below.

---

## Operational notes

- **Payload size.** DynamoDB items are capped at 400 KB. The `bytes` attribute
  written by `write()` lets you alert on growth. Large payloads should be
  offloaded through `ContextPolicyMW` before checkpointing, not smuggled into
  the state dict.
- **Checkpoint id monotonicity.** The backend maintains an in-process sequence
  counter per `session_id` to produce monotonically-increasing ids
  (satisfies `CKPT-C-01`). If you run multiple containers behind a load
  balancer for the *same* session, swap the counter for a UUIDv7 or ULID so
  ordering is globally stable.
- **Deletion.** `delete_session` paginates the query + `BatchWriteItem` result
  so sessions with ≥ 25 checkpoints delete in 25-item chunks (the AWS API
  limit). For > 10k checkpoints, consider table-level cleanup via TTL on
  `created_at` — cheaper at scale.
- **Security.** The payload bytes round-trip via `pickle`. Only deploy this
  backend in a trust boundary where the writing process is also the reading
  process. Swap to `json.dumps`/`loads` if you want an attack-surface-smaller
  payload format.

---

## Layout

```
examples/extension-dynamodb-checkpointer/
├── Dockerfile                           # copy of Dockerfile.extension-template
├── README.md                            # this file
├── pyproject.toml                       # entry-point declaration
├── src/
│   └── dynamodb_ckpt/
│       ├── __init__.py                  # re-exports DynamoDBCheckpointer
│       └── checkpointer.py              # ~100 LOC implementation
└── tests/
    ├── __init__.py
    └── test_dynamodb_ckpt_contract.py   # one-line contract hookup
```

---

## License

MIT (inherits from monkey-bot).
