# marketing-bot (pre-feature reference config)

This directory is a frozen snapshot of what a GCP-based monkey-bot deployment
looked like **before** the `harness-extensibility` feature. It is the input for
the Story 10 regression gate (`tests/e2e/test_marketing_bot_regression.py`) and
proves that an existing consumer's `HarnessConfig` still loads, still builds an
agent, and still resolves to the shipped GCP defaults (`FirestoreCheckpointer`,
`FirestoreMemoryStore`, `VertexProvider`) with **zero** extension-specific
fields in their YAML.

Do not add `checkpointer`, `memory_store`, `job_storage`, `identity_source`, or
`model_provider` blocks here — their absence is the whole point.
