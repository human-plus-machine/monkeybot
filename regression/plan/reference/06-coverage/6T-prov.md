# 6.T Providers (`PROV`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`.

Existing: `tests/providers/*` mock SDK objects in process (MagicMock events, fake modules in `sys.modules`). No recorded vendor bytes anywhere. OpenRouter/Fireworks/NVIDIA/HF have constructor tests only. Event order differs per vendor and nothing asserts it.

| ID | Case | Asserts |
|---|---|---|
| T1-PROV-01 | `converter_conformance` | Canonical conversation (system, user, assistant text + 2 parallel `ToolRequest`s, `ToolResponse` with text, image, PDF, `Thinking` with signature) through `messages_to_openai`, `build_anthropic_messages`, `_messages_to_contents`, `messages_to_converse`: roles ordered; tool results paired by id; thinking replayed or dropped as documented per adapter; media in the vendor slot or a documented placeholder. |
| T1-PROV-02 | `tool_arg_fragment_conformance` | Same fragment list (valid object, empty, truncated, JSON array, split mid-string) through Anthropic's buffer, Converse `_ToolBuf`, OpenAI slots, Gemini `_merge_function_call_args`: identical `(args, parse_error)`. |
| T1-PROV-03 | `stream_order_contract` | Per adapter, the documented event order (text/thinking/tool/usage/done) is asserted, so a reorder is a visible change. |
| T1-PROV-04 | `usage_semantics` | OpenAI `input = prompt - cached`; Anthropic cached = read + creation; Gemini omits usage when missing (pinned); zero-usage event shapes. |
| T1-PROV-05 | `error_mapping_table` | Rate limit → `ProviderRateLimitError` after 3; 5xx → `ProviderServerError`; tool-unsupported → one tool-less retry; Ollama body-read 400 → media-stripped retry; **no retry after the first yielded event**. |
| T1-PROV-06 | `sampling_strip_retry` | `temperature` / `thinking` / `output_config` stripped on blame; no retry after content starts. |
| T1-PROV-07 | `token_count_fallbacks` | Vertex/Bedrock fall back to estimates when the API refuses; compat providers use tiktoken (pinned). |
| T1-PROV-08 | `cache_hints` | `cache_retention` none/short/long → Anthropic markers / OpenAI `prompt_cache_retention="24h"` / nothing for Gemini; session affinity header for OpenAI and Claude only. |
| T1-PROV-09 | `compat_providers_stream` | NVIDIA, OpenRouter, Fireworks, HuggingFace each drive `Provider.stream` through a fake chunk iterator (today only constructors are tested). |
| T1-PROV-10 | `request_byte_budget` | Media trimmed before PDF extraction; `RequestByteBudgetError` when text alone exceeds the cap. |
| T3-PROV-01 | `openai_tool_split_with_and_without_index` | Wire cassette. |
| T3-PROV-02 | `nvidia_hermes_tool_call_split` | `<tool_call>` across SSE data lines. |
| T3-PROV-03 | `anthropic_input_json_and_signature` | `input_json_delta` + `signature_delta` + `max_tokens` stop. |
| T3-PROV-04 | `gemini_partial_args_thought_signature_no_usage` | Second-choice replay path. |
| T3-PROV-05 | `converse_tool_input_string_and_object` | Second-choice replay path. |
| T3-PROV-06 | `thinking_only_completion_per_family` | Each family yields thinking then `Done` with no text. |
| T3-PROV-07 | `ollama_cloud_glm_tool_turn` | The model every live run uses; recorded with `OLLAMA_API_KEY`. Also covers the shared `_openai_compat` parser. First T3b cassette to record. |
| T3-PROV-08 | `staleness_check` | Installed SDK versions match cassette headers or a re-record exists. |
| T4-PROV-01 | `provider_live_matrix` (live preflight) | Ollama Cloud always (runs first in every live suite): one `stream` with a trivial tool + `count_input_tokens`; sequence type-checks. Any other provider only if its key is set; otherwise skipped with the reason. |
