# Q&A Log — monkeybot-v2-e3

## Phase 1A — Discovery & Core Design

**Q: Should `record_usage()` be called inside `loop.py` (via injected callback) or by the gateway layer after consuming `TurnComplete`?**  
A: Gateway layer. The loop's responsibility is event production; the caller handles side effects. Consistent with how history.save() is called by the loop only because it's needed for the *next* provider call — not as a general principle. Usage recording is not needed within the turn, only after it.

**Q: Should `Scheduler` run as an `asyncio.Task` or a separate thread?**  
A: `asyncio.Task`. The serve command already runs in an asyncio event loop (uvicorn). asyncio.Task is the natural primitive — avoids thread-safety concerns and allows job callables to be coroutines.

**Q: Should `croniter` be truly optional (fall back silently) or required when scheduler is used?**  
A: Silently optional with a one-time warning. `[scheduler]` optional extra already wired in pyproject.toml. Without croniter, fall back to `+1 hour` intervals so basic scheduling still works.

**Q: Should `turn_usage` go in the same SQLite file as `messages` or a separate DB?**  
A: Same file (`monkeybot.db`). No operational reason to split; joins between tables are useful; simpler backup/restore story.

**Q: Should Scheduler job entries in config.yaml specify shell commands or Python callables?**  
A: Python callables (dotted `module:function` path). Avoids shell injection risk, keeps jobs first-class async Python, consistent with the codebase's zero-shell-in-config philosophy.

**Q: The asyncio scheduler is incompatible with serverless (Lambda, AgentCore, Agent Engine). Should E3 be redesigned to support both, or deferred to a future epic?**  
A: Deferred to E5. The serverless problem is not just the scheduler — the SQLite persistence layer (ConversationHistory, turn_usage, job_runs) is also incompatible with ephemeral/multi-instance runtimes. Fixing it properly requires a StorageBackend protocol abstraction across E1+E3, plus DynamoDB/Firestore adapters. That is a cross-cutting redesign, not an E3 concern. E3 is scoped to long-running process deployments (containers, VMs). E5 "Serverless Portability & Pluggable Storage" tracks the full serverless solution. 1C risk section updated with explicit serverless constraint. epic-breakdown.md updated with E5 epic.
