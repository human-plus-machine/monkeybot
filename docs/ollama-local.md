# Local Ollama prefix cache

Monkeybot composes a **stable system prefix** (`AGENT.md` + harness) plus a **volatile tail** (date, memory, skills). Cloud providers reuse that prefix via prompt cache. Local Ollama reuses it only as an in-memory **KV prefix** while the model stays loaded, and only if the rendered prompt prefix is byte-identical.

The cold **first** prefill of the system prompt and tool schemas is always paid. This page is about not paying it again on every tool step.

## What monkeybot sends

For `model.provider: ollama-local` (and legacy `ollama` when the host is local):

| Knob | YAML | Default |
|---|---|---|
| Keep model loaded | `model.keep_alive` | `24h` |
| Pin llama context | `model.num_ctx` | omitted (server / Modelfile default) |

Cloud mode (`ollama-cloud`) sends neither. These keys are YAML-only — they are not mapped from the runtime env.

`keep_alive: "0"` (or empty) omits the field.

These go out as OpenAI-compat `extra_body`: top-level `keep_alive`, and `options.num_ctx` when pinned. If an older Ollama build ignores `/v1` `keep_alive`, set the **server** env `OLLAMA_KEEP_ALIVE=24h` on `ollama serve` as well. That is an Ollama daemon setting, not a monkeybot runtime env.

## `context_window` is not `num_ctx`

`model.context_window` (default `1000000`) is the summarization/spill budget. It is **never** copied onto Ollama `num_ctx`. Mapping a million-token window onto a local runner makes prefill crawl and can reload the model. If you need a pinned context, set `num_ctx` explicitly (e.g. `8192`) and keep it stable across requests — changing it unloads the model and drops the prefix cache.

## Chat template: tools must stay in the system block

Many stock Ollama templates only render `{{ .Tools }}` when the last message is a user turn (`{{- if and $.Tools $last }}`). After a tool result that block vanishes; on the next user turn it reappears somewhere else. Prefix cache miss. Full re-prefill of the tool schemas.

Pin tools in the system block with the example Modelfile:

```bash
# Edit FROM to your base tag, then:
ollama create llama3.1:8b-prefix-stable -f examples/ollama/PrefixStable.Modelfile
```

Point `model.name` at the new tag. Monkeybot does not run `ollama create` for you.

## GGUF vs MLX (Mac)

The llama.cpp runner reuses prefixes between agent steps. MLX packs (`*-mlx` tags) often re-prefill the whole prompt even when the prefix matches ([ollama#17829](https://github.com/ollama/ollama/issues/17829)). Prefer GGUF tags for tool-calling loops. `monkeybot doctor` warns when `model.name` looks like MLX.

## Thinking delay

`thinking_budget: -1` (default) leaves Ollama thinking on for models that support it (Gemma 4, Qwen3, …). That generates reasoning tokens **after** prefill and **before** the first visible reply. Set `thinking_budget: 0` to send `reasoning_effort: none`. `monkeybot doctor` only warns about the default on known reasoning tags.

## Expected cache misses

`enable_mcp` adding tools changes the tools array; that is a new prefix. Compaction / a new context epoch also starts a fresh prefix. `doctor` check ids: `ollama.local.mlx_runner`, `ollama.local.thinking_default`, `ollama.local.num_ctx_invalid`, `ollama.local.num_ctx_large`.
