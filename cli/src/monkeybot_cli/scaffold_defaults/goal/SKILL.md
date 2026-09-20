---
name: goal
description: Create one durable objective that continues across scheduled turns until completed.
---

# Goal

Use this skill only when the user explicitly invokes `/goal` with an objective
that may require multiple turns.

## Start

1. Preserve the user's complete objective without narrowing its scope.
2. Call `create_goal` exactly once with that objective.
3. Do the first concrete increment of work in the same turn.
4. Keep the returned goal id; every `update_goal` call must include it.

## Continue

- Goal continuations are scheduled automatically.
- Treat the stored objective as authoritative.
- Make measurable progress and verify results before declaring completion.
- Temporary session-busy deferrals are normal and are not goal failures.
- Persistent execution failures stop the goal after the retry limit so it
  cannot consume resources indefinitely.

## Complete

Call `update_goal` with the returned `goal_id` and `status="complete"` only
after evidence proves every requirement is satisfied. Otherwise leave the goal
active. Use the `/goals` control plane to pause, resume, or stop it manually.
