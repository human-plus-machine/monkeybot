---
name: goal
description: Create one durable objective that continues across scheduled turns until evidence proves it is done.
---

# Goal

Use this skill only when the user explicitly invokes `/goal` with an objective
that may require multiple turns.

A goal is complete only when current evidence proves every requirement of the
objective. Stopping a turn, running out of time, or having a plausible answer
does not finish it.

There is no deadline that finishes the work. The goal stays active across turns
until the end state is true, or until a budget pause or a persistent failure
stops the ticks. A budget pause is not completion. The objective stays intact
and the user can resume it.

## Start

1. Preserve the user's complete objective without narrowing its scope.
2. Call `create_goal` exactly once with that objective.
3. Do the first concrete increment of work in the same turn.
4. Keep the returned goal id. Every `update_goal` call must include it.

## Continue

- Goal continuations are scheduled automatically. A webhook can also wake the goal.
- Treat the stored objective as authoritative. Do not shrink it to work already done.
- Make measurable progress. Verify results before you even consider completion.
- Temporary session-busy deferrals are normal and are not goal failures.
- Persistent execution failures stop the goal after the retry limit.
- Using up the tick or runtime budget pauses the goal. It does not complete it.

## Waiting

If you are blocked on a person or an external event (review, approval, CI, a
publish), call `update_goal` with `status="waiting"`, the goal id, and a
`reason`. Pass `recheck_after` (for example `2h`) only when you should look
again even if no event arrives. Do not keep waking on the timer while blocked.
You cannot pause a goal. That is the user's control.

## Complete

Before `update_goal` with `status="complete"`, completion is unproven. In a
separate step, after the evidence tool calls:

1. Pull concrete requirements out of the objective and anything it points at
   (files, plans, specs, issues, instructions). Do not shrink the original scope.
2. For each requirement, name the evidence that would prove it, then look at
   the current state: files, command output, tests, pull-request state, rendered
   UI, runtime behavior.
3. Judge each item as `proved`, `contradicted`, `incomplete`, `too_weak`, or
   `missing`. A green test counts only if it covers that requirement. Uncertain
   or indirect evidence is not done.
4. The audit has to prove completion. Failing to notice leftover work is not enough.

Intent, partial progress, memory of earlier turns, and a reasonable-sounding
answer are not proof.

Call `update_goal` with `status="complete"` and an `audit` array only after
every item is `proved` and no required work remains. The call must be its own
step so the transcript already contains the evidence. Ending the turn does not
call `update_goal`. The goal stays active and the full objective stays intact.

`status="active"` is only for resuming a goal the user paused.
