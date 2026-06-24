---
name: acceptance
description: Phase 7 - Acceptance. Executes the acceptance checklist drafted in Phase 2B. Agent runs all agent-automatable checks (curl, CLI) and presents human-ui and human-verify items as a structured sign-off checklist. Feature is marked complete only after every check passes.
---

# Phase 7: Acceptance

## Purpose

Confirm the feature is fully working from the outside — not just that the code matches the spec, but that real users and operators can use it as intended. This is the final gate before the feature is marked `completed`.

Phase 7 works through the `2b-acceptance.md` checklist systematically:
1. Agent runs all `agent-automatable` checks and reports results
2. Agent presents `human-ui` and `human-verify` items as a structured checklist the human works through
3. Feature is marked `completed` only when every item is confirmed

## When This Phase Runs

Phase 7 begins after Phase 6 (Integration) is complete:
- All stories have status `integrated`
- Full test suite passes
- Linter is clean
- User has confirmed readiness to proceed

## Prerequisites

Before starting, verify:
1. The application is running locally (or a test environment is available)
2. Test data / seed scripts are available if needed
3. `stories/2b-acceptance.md` exists and was approved in Phase 2B

If `stories/2b-acceptance.md` does not exist, stop and inform the user — they must run Phase 2B before Phase 7 can proceed.

## Orchestrator Workflow

### Step AC1: Load the Checklist

Read `.monkeymode/{feature-name}/stories/2b-acceptance.md` in full.

Parse all items and categorize by type:
- `agent-automatable` — agent runs these directly
- `human-ui` — human performs these in the browser
- `human-verify` — agent triggers, human confirms result

Report to user:
```
"Phase 7 — Acceptance. I'll now work through the acceptance checklist.

  agent-automatable: {N} items (I'll run these)
  human-ui:          {N} items (you'll perform these)
  human-verify:      {N} items (I'll trigger, you confirm)

Starting with automated checks now."
```

### Step AC2: Run Agent-Automatable Checks

Execute each `agent-automatable` item in order. For each:

1. **Run the command(s)** exactly as specified in the checklist
2. **Compare output** to the expected result — check status code, response body fields, CLI output
3. **Record result:** `PASS` or `FAIL`
4. **On failure:** Do not stop — record the failure and continue to the next item. Report all failures together at the end of the automated pass.

After all automated checks complete, report:

```
"Automated checks complete:

PASS  AC-001: Add a product to favorites (201, correct response body)
PASS  AC-002: Get favorites list returns paginated results (200, 10 items)
FAIL  AC-003: Duplicate favorite returns 409 (got 500 instead)
PASS  AC-004: Unauthenticated request returns 401
...

Automated: {N} passed, {N} failed.
```

If any automated checks failed, stop and ask the user:
```
"{N} automated check(s) failed. Would you like me to investigate and fix these before continuing
to the manual checks? (Recommended — manual checks may depend on this working correctly.)"
```

If the user says fix: load `phases/rework.md` and follow the structured rework process, targeting the failing acceptance check back to its origin phase. Re-run the failed checks after the fix.

If the user says continue anyway: note the failures and proceed.

### Step AC3: Human Sign-Off Checklist

Present all `human-ui` and `human-verify` items as a structured checklist. For `human-verify` items, run the agent-side setup steps first, then hand off to the human.

Present items one at a time or as a grouped list — match the user's preferred pace. For each:

```
"AC-012 (human-ui): Add item to favorites from product listing page

Steps:
1. Log in as test@example.com
2. Navigate to /products
3. Hover over any product card
4. Click the heart icon

Expected result:
- Heart icon changes from outline to filled
- Toast notification: 'Added to favorites'
- Icon remains filled after page refresh

Ready when you are. Reply 'pass', 'fail', or 'skip' (with a note)."
```

Record each response in the acceptance state.

### Step AC4: Triage Failures

After all items are checked:

**If all items pass:** Proceed to Step AC5.

**If any items failed:**

1. Group failures by type:
   - **Implementation bugs** — Something that should work doesn't (e.g. wrong status code, missing field). Fix via rework loop.
   - **Design gaps** — The feature works as specced but the spec missed a real user need (e.g. no empty state message). Fix via rework guide.
   - **Environment issues** — Only fails locally due to config/seed data (e.g. missing env var). Fix the environment, re-run the check.

2. Present grouped failures to user:
```
"Acceptance failures:

Implementation bugs (need rework):
  - AC-003: Duplicate favorite returns 500 instead of 409
  - AC-015: Pagination breaks when favorites > 20

Design gaps (spec update needed):
  - AC-018: No empty state shown when favorites list is empty

Environment issues (local config):
  - AC-007: Migration fails — DATABASE_URL not set in .env.test

How would you like to proceed?"
```

3. For implementation bugs and design gaps: load `phases/rework.md` and trace each failure to its origin phase.
4. After fixes are made, re-run only the failed items — do not re-run the entire checklist.

### Step AC5: Final Report and Sign-Off

When all items pass (or user accepts remaining items as known issues):

```
"Phase 7 — Acceptance complete.

Results:
  Automated checks:  {N}/{N} passed
  Human checks:      {N}/{N} passed
  Known issues:      {N} accepted by user (see notes)

Acceptance checklist: .monkeymode/{feature-name}/stories/2b-acceptance.md

All {total} acceptance criteria confirmed. The feature is fully implemented,
verified, integrated, and accepted.

Marking {feature-name} as completed."
```

### Step AC6: Update State

```json
{
  "current_phase": "completed",
  "phase_status": {
    "acceptance": "completed"
  },
  "acceptance": {
    "status": "completed",
    "automated_passed": 12,
    "automated_failed": 0,
    "human_passed": 8,
    "human_failed": 0,
    "known_issues": [],
    "completed_at": "ISO8601 timestamp"
  }
}
```

If there are accepted known issues:
```json
{
  "acceptance": {
    "known_issues": [
      {
        "id": "AC-018",
        "description": "No empty state shown — accepted as follow-up work",
        "accepted_by": "user",
        "accepted_at": "ISO8601 timestamp"
      }
    ]
  }
}
```

## Rework from Phase 7

If a Phase 7 failure requires rework, follow `phases/rework.md` to trace the failure to its origin:

| Failure Type | Likely Origin Phase | Fix |
|---|---|---|
| Wrong HTTP status code | Phase 3 (Code Spec) or Phase 4 (Implementation) | Fix spec or implementation |
| Missing API field | Phase 1B (Contracts) or Phase 3 | Update contract and spec |
| UI doesn't match expected behavior | Phase 4 (Implementation) | Fix implementation |
| Empty state missing | Phase 1A (Discovery) — requirement missed | Add to design, cascade through phases |
| Performance issue under real load | Phase 1C (Operations) | Update perf spec, re-implement |

After rework, re-run Phase 4+ for affected stories, then come back to Phase 7 for re-verification.

## Notes on Environment

Phase 7 assumes the application is running. The agent uses the commands exactly as written in `stories/2b-acceptance.md` — it does not modify or adapt them. If a command fails due to environment setup (wrong port, missing env var, missing seed data), the agent will flag it as an environment issue and ask the user to fix the environment before re-running.

The agent will NOT attempt to start, restart, or configure the application environment — that is the user's responsibility before invoking Phase 7.
