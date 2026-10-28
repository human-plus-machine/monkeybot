# 6. Feature coverage matrix

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Each area lists: what already covers it, the gaps found, then the cases to add. "Existing" paths are today's paths (`tests/core/...`); section 4.3 gives where each one moves. "Migrate" means the case exists in `tests/evals/` or `evals/` and moves into the new schema. Cases with no T4/T5 entry are harness logic that a live model should not be needed for.
