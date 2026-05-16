---
name: verification
description: Phase 5 - Verification. Orchestrates verification of implemented stories against their code specs, design docs, and acceptance criteria. Uses verifier subagents in parallel, with a reworker loop for failures.
---

# Phase 5: Verification

## Purpose

Verify that each story's implementation fully matches its code spec, design docs, and acceptance criteria before proceeding to integration. This phase catches implementation gaps, signature mismatches, missing error handling, and quality issues at the story level — where they are cheapest to fix.

## When This Phase Runs

Phase 5 begins after Phase 4 (Implementation) completes for all stories. The orchestrator enters this phase when:

- All stories have status `implementation_complete`
- The full test suite passes
- The linter is clean

## Orchestrator Workflow

### Step V1: Identify Stories Ready for Verification

Read `state.json` and find stories that have completed implementation but not yet been verified:

```
For each story in state.stories:
  IF story.status == "implementation_complete"
  THEN -> eligible for verification
```

### Step V2: Spawn Verifier Subagents

For each eligible story, spawn a `verifier` subagent (`subagent_type: "verifier"` via the Task tool).

**How to build the verifier prompt:**
1. Include story-specific context (see **Verifier Prompt Template** below) in the subagent's `prompt` parameter and launch all verifiers for a batch in a **single message** (parallel tool calls)

**CRITICAL RULES:**
- Verifier subagents are **read-only** — they do NOT modify any files
- Never exceed 10 concurrent subagents
- Each verifier gets a complete, self-contained prompt
- **Do NOT pass a `model` parameter** when spawning subagents via the Task tool. Omit it entirely so subagents inherit the parent conversation's model. Never use `model: "fast"` for verification or rework — these require the full-capability model.

### Step V3: Collect Verification Results

Each verifier returns a structured report with:
- **Overall status:** pass | pass-with-warnings | fail
- **Acceptance criteria results** (per-criterion PASS/PARTIAL/FAIL)
- **Signature mismatches, missing files, missing tests, quality issues**

### Step V4: Triage Results

For each story's verification result:

```
IF overall_status == "pass":
  -> Mark story as "verified"
  -> Story is ready for Phase 6 (Integration)

IF overall_status == "pass-with-warnings":
  -> Present warnings to user
  -> User decides: proceed to integration or fix warnings first
  -> If proceed: mark as "verified"
  -> If fix: enter rework loop (Step V5)

IF overall_status == "fail":
  -> Enter rework loop (Step V5)
```

### Step V5: Rework Loop

When verification fails, the orchestrator spawns a `reworker` subagent (`subagent_type: "reworker"`) to fix the issues.

```
WHILE story.status != "verified" AND rework_attempts < max_rework_attempts:
  1. Spawn reworker subagent (subagent_type: "reworker") with:
     - The verification report (full failure details)
     - The code spec
     - File boundaries
  2. Collect reworker results
  3. IF reworker escalated (spec-level issue):
     -> Load phases/rework.md and follow the structured rework process
     -> This may cascade back to Phase 3 or earlier
     -> After spec rework, re-implement, then re-verify
  4. IF reworker completed:
     -> Re-verify by spawning verifier subagent again
     -> Go back to Step V4 with new results
  5. Increment rework_attempts
```

**Maximum rework attempts:** 3 per story. If a story fails verification 3 times:
- Mark as `verification_failed`
- Present to user with full history of attempts
- User decides: run the structured rework guide, skip the story, or investigate deeper

### Step V5b: Audit Test Corrections

Before triaging final results, the orchestrator must check whether the implementer made any test corrections under the Option B escape hatch.

Read `state.json` for each story's `verification.test_corrections` array. For each logged correction, the verifier subagent must specifically check it.

**Add to the verifier prompt** for any story with test corrections:

```
## Test Corrections to Audit

The implementer modified the following tests under the escape hatch. Verify each correction
is justified by the code spec. For each:
- APPROVED: The original test contradicted the spec — the correction is valid
- REJECTED: The correction was not justified by the spec — flag as a verification failure

{paste the test_corrections array from state.json for this story}
```

**Correction audit outcomes:**
- All corrections **approved** → proceed normally (note in report)
- Any correction **rejected** → treat the story as `fail` — the implementer changed a test to hide a real bug; the reworker must restore the original test and fix the implementation

### Step V6: Report to User

After all stories in a batch are verified (or failed):

```
"Phase 5 — Verification complete:

Story 1: Embeddings Component — PASS (all 8 acceptance criteria met)
Story 2: Vector Store — PASS WITH WARNINGS (missing docstring on 1 helper function)
Story 3: Storage Component — PASS (fixed after 1 rework cycle: missing error handling)
Story 4: Config Service — FAILED (3 rework attempts, spec-level issue needs Phase 3 rework)

Ready to proceed to Phase 6 (Integration) with Stories 1-3?"
```

**CRITICAL: Always ask user for confirmation before proceeding to Phase 6.**

### Step V7: Update State

After verification completes, update state.json:

```json
{
  "stories": {
    "story-1-embeddings": {
      "status": "verified",
      "verification": {
        "result": "pass",
        "rework_attempts": 0,
        "last_checked_at": "ISO8601 timestamp"
      }
    },
    "story-3-storage": {
      "status": "verified",
      "verification": {
        "result": "pass",
        "rework_attempts": 1,
        "rework_summary": "Added missing error handling for S3 connection timeout",
        "last_checked_at": "ISO8601 timestamp"
      }
    },
    "story-4-config": {
      "status": "verification_failed",
      "verification": {
        "result": "fail",
        "rework_attempts": 3,
        "escalated_issues": ["Spec says return 404, but design contract requires 409 for duplicates"],
        "last_checked_at": "ISO8601 timestamp"
      }
    }
  }
}
```

## Verifier Prompt Template

**Each verifier subagent receives a prompt composed of two parts:**

**Story-specific context** — Include the following template in the subagent's `prompt` parameter.

```
## Story to Verify

**Story:** {story_title}
**Story ID:** {story_key}
**Feature:** {feature_name}

## Code Spec

{paste the FULL contents of the story's code spec file}

## Files to Read on Startup

Before verifying any code, read these files to load context:

**Design context** (read the sections referenced by the code spec above):
- {workspace}/.monkeymode/{feature-name}/design/1a-discovery.md — sections: {list relevant section names}
- {workspace}/.monkeymode/{feature-name}/design/1b-contracts.md — sections: {list relevant section names}
- {workspace}/.monkeymode/{feature-name}/design/1c-operations.md — sections: {list relevant section names}

## Implemented Files

**Files created:**
{list of files that were created during implementation}

**Files modified:**
{list of files that were modified during implementation}

**Test files:**
{list of test files created}

## Test Results

{paste the output of running the test suite — pass/fail counts}

## Linter Results

{paste linter output — clean or list of errors}
```

## Reworker Prompt Template

**Each reworker subagent receives a prompt composed of two parts:**

**Rework-specific context** — Include the following template in the subagent's `prompt` parameter.

```
## Story to Rework

**Story:** {story_title}
**Story ID:** {story_key}
**Feature:** {feature_name}

## Verification Report

{paste the FULL verification report from the verifier subagent}

## Code Spec

{paste the FULL contents of the story's code spec file}

## Files to Read on Startup

Before making any fixes, read these files to load context:

**Design context** (read the sections referenced by the code spec above):
- {workspace}/.monkeymode/{feature-name}/design/1b-contracts.md — sections: {list relevant section names}

**Language-specific coding guidelines** (pick ONE based on the project's language):
- Python: monkeymode/guides/PYTHON-CODING-GUIDELINES.md
- Java: monkeymode/guides/JAVA-CODING-GUIDELINES.md
- Angular: monkeymode/guides/ANGULAR-CODING-GUIDELINES.md
- .NET/C#: monkeymode/guides/DOTNET-CODING-GUIDELINES.md
- React: monkeymode/guides/REACT-CODING-GUIDELINES.md
- Terraform: monkeymode/guides/TERRAFORM-CODING-GUIDELINES.md

## File Boundaries

You may ONLY modify these files:

**Source files:**
{list of source files from the story}

**Test files:**
{list of test files from the story}

Do NOT modify state.json.
Do NOT modify files belonging to other stories.
Do NOT commit changes.

## Previous Rework Attempts (if any)

{paste summaries of previous rework attempts so the reworker can learn from them}
```