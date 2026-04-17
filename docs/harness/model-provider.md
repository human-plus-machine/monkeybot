# Model providers

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Contract shapes are defined in `src/core/harness/extensions/specs/model_provider.py` and exercised by `tests/harness/extensions/model_providers/`.

A `ModelProvider` is the factory the assembler calls to materialize a
`BaseChatModel`. One provider per invocation is the norm; subagent
recursion may spawn a different provider per node if configured.

## Shipped providers

| Provider | Import path | Install extra | Notes |
|---|---|---|---|
| `VertexProvider` | `emonk.core.harness.extensions.model_providers:VertexProvider` | (core) | GCP-native; Gemini on Vertex AI |
| `BedrockProvider` | `emonk.core.harness.extensions.model_providers:BedrockProvider` | `emonk[model-provider-bedrock]` | Wraps `langchain-aws.ChatBedrockConverse` — use the Converse schema, not the deprecated `ChatBedrock` |
| `OpenAIProvider` | `emonk.core.harness.extensions.model_providers:OpenAIProvider` | `emonk[model-provider-openai]` | OpenAI public API |
| `AnthropicProvider` | `emonk.core.harness.extensions.model_providers:AnthropicProvider` | `emonk[model-provider-anthropic]` | Direct Anthropic API; use `vertex-anthropic` extra for Vertex-hosted Claude |
| `OllamaProvider` | `emonk.core.harness.extensions.model_providers:OllamaProvider` | `emonk[model-provider-ollama]` | Local / self-hosted models |

Non-shipped (build yourself): Azure OpenAI, SageMaker, vLLM, LiteLLM
gateway. Subclass `ModelProvider.build()` and return any
`BaseChatModel`-shaped object.

## Per-backend wiring recipes

### Bedrock (AWS enterprise stack)

```python
from emonk.core.harness.extensions.model_providers import BedrockProvider

provider = BedrockProvider(
    region="us-east-1",
    model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
    guardrail_id=None,  # optional — dry-run via /harness/aws/smoke first
)
```

IAM requirements: `bedrock:InvokeModel` +
`bedrock:InvokeModelWithResponseStream` on the foundation model ARN.
`BedrockProvider` uses `ChatBedrockConverse`; the contract test
`MP-C-03` pins the tool-use schema to the Converse shape so a `boto3` drift
fails CI loudly.

### OpenAI

```python
from emonk.core.harness.extensions.model_providers import OpenAIProvider

provider = OpenAIProvider(
    api_key_handle="OPENAI_API_KEY",   # resolved through SecretResolver
    model="gpt-4o",
    temperature=0.2,
)
```

The handle is resolved at `build()` time, so rotating the key only requires
a cache bust on the `SecretResolver`.

### Anthropic (direct)

```python
from emonk.core.harness.extensions.model_providers import AnthropicProvider

provider = AnthropicProvider(
    api_key_handle="ANTHROPIC_API_KEY",
    model="claude-3-5-sonnet-latest",
)
```

For **Claude on Vertex**, install `emonk[vertex-anthropic]` and use
`VertexProvider` with a Claude model id — the Google auth path is the same
as Gemini.

### Vertex

```python
from emonk.core.harness.extensions.model_providers import VertexProvider

provider = VertexProvider(
    project="your-gcp-project-id",
    location="us-central1",
    model="gemini-2.5-pro",
)
```

Uses Application Default Credentials. Set
`GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json` for local development.

### Ollama (local / air-gapped)

```python
from emonk.core.harness.extensions.model_providers import OllamaProvider

provider = OllamaProvider(
    base_url="http://llm-gateway.internal:11434",
    model="llama3.1:70b",
)
```

Great default for on-prem Postgres stacks: zero external dependency, same
`BaseChatModel` shape the rest of the framework expects.

## Capabilities

Every provider returns a `ModelCapabilities` shape from
`.capabilities()`. The assembler validates the reported truth against the
caller's requirements (tool calling, streaming, thinking, vision) and
raises `BackendCapabilityMismatch` if they collide.

```python
caps = provider.capabilities()
# ModelCapabilities(
#     tool_calling=True, streaming=True,
#     thinking=False, vision=True, max_context_tokens=200_000,
# )
```

Add a capability to your custom provider by overriding `capabilities()` and
the assembler will pick it up for free.

## Choosing a provider

- **GCP agents** → `VertexProvider` (or `AnthropicProvider` on Vertex).
- **AWS agents** → `BedrockProvider` (Converse path); `OpenAIProvider` via
  AWS PrivateLink if compliance prefers.
- **On-prem** → `OllamaProvider`; fall back to an internal LiteLLM gateway
  by shipping a custom `LiteLLMProvider` (~50 LOC) if you need fleetwide
  routing.

## Runnable snippet

```python
# Instantiate the mock provider and invoke it — no cloud credentials required.
from emonk.core.harness.extensions._mocks import MockModelProvider
from emonk.core.harness.specs import AgentSpec

provider = MockModelProvider()
model = provider.build(AgentSpec(name="demo"))
print(model.invoke("hello"))
print("capabilities:", provider.capabilities())
```
