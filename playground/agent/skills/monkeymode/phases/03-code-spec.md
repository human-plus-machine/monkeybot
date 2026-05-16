---
name: code-spec-skill
description: Phase 3 - Code Spec. Orchestrator guide for spawning code-spec-writer subagents in parallel, each of which writes its spec directly to disk. The orchestrator reviews written specs with the user sequentially, then updates state.json before advancing to Phase 4.
---

# Phase 3: Code Spec — Orchestrator Guide

## Purpose

Orchestrate the creation of code specs for all user stories before Phase 4 begins. The orchestrator spawns `code-spec-writer` subagents (up to 10 in parallel) — each subagent investigates the codebase and **writes its spec file directly to disk**. The orchestrator then reads each written spec, presents it to the user for approval, and updates `state.json`.

## Parallel Execution — Orchestrator Workflow

Phase 3 runs as a **two-step pipeline across all stories**: subagents write specs in parallel, then the orchestrator presents each written spec to the user sequentially for approval.

The main agent acts as the **orchestrator** — it never writes specs directly. It spawns `code-spec-writer` subagents via the Task tool and manages the review and state-update flow.

### Architecture Overview

```
User
  |
  v
Orchestrator (main agent)
  |  - Reads state.json and user_stories.md
  |  - Builds self-contained prompts per story
  |  - Spawns up to 10 code-spec-writer subagents in parallel
  |
  |  ── Step 1: Writing Pass (parallel, all stories at once) ──
  |---> code-spec-writer 1  -> Story A (investigates codebase, writes spec to disk)
  |---> code-spec-writer 2  -> Story B (investigates codebase, writes spec to disk)
  |---> ...up to 10        -> Story N (investigates codebase, writes spec to disk)
  |
  |  ── Step 2: Review Pass (sequential, one spec at a time) ──
  |  Orchestrator reads structured output (summary + open_questions) from subagent response
  |  Orchestrator resolves open_questions with user if any
  |  Orchestrator presents summary for user approval
  |  Orchestrator updates state.json on approval
  |
  v
All stories have status: "code_spec" and files_to_create/files_to_modify populated
  |
  v
Phase 4 (Implementation) — conflict detection uses the file lists
```

### Orchestrator Step-by-Step

#### Step O1: Collect Stories

Read `state.json`. Identify all stories that need code specs — status is `not_started` or `code_spec` not yet complete. This is typically every story from Phase 2.

If ZERO stories need specs, check whether all are already `code_spec` status and advance to Phase 4.

#### Step O2: Build Subagent Prompts

For each story, assemble a self-contained prompt containing:
- Full user story text (from `user_stories.md`)
- All acceptance criteria (from `2b-acceptance.md`)
- Paths to the three design docs (`1a-discovery.md`, `1b-contracts.md`, `1c-operations.md`)
- Language guidelines path (e.g. `guides/PYTHON-CODING-GUIDELINES.md`)
- The exact output path where the subagent must write the spec: `{workspace}/.monkeymode/{feature-name}/code_specs/{story-id}-spec.md`
- Specific codebase reference files to investigate (similar services, repositories, test files — identified from Phase 1 design docs)
- Any conventions already confirmed in Phase 1 (or instruct subagent to discover them)
- Explicit out-of-scope items from the user story

Use the prompt template below.

#### Step O3: Spawn code-spec-writer Subagents (Parallel)

Launch one `code-spec-writer` subagent per story using the Task tool (`subagent_type: "code-spec-writer"`). Each subagent writes its spec directly to the path provided in the prompt.

**CRITICAL RULES:**
- Launch all subagents in a **single message** (parallel tool calls)
- **Max 10 concurrent subagents** — if there are more than 10 stories, batch them 10 at a time; complete each batch before spawning the next
- Each subagent gets a **complete, self-contained prompt** — subagents have NO access to conversation history
- **Do NOT pass a `model` parameter** — omit it so subagents inherit the parent's model

#### Step O4: Collect Structured Output and Resolve Open Questions

After subagents complete, for each story read the **Structured Output JSON** returned by the subagent:

```
spec_ready        true | false
summary           short description of what the spec covers
files_to_create   list of file paths
files_to_modify   list of file paths
open_questions    list of { id, question, blocking, default_if_not_blocking }
```

**If `spec_ready: false`** — surface each `blocking: true` question to the user, collect answers, and instruct the subagent (or apply edits directly) to update the spec file with the answers, then present for approval.

Non-blocking questions with defaults may be accepted as-is unless the user wants to override.

#### Step O5: Present Specs for User Approval (Sequential)

Even though writing is parallel, present specs to the user **one at a time** using the subagent's returned `summary` and `open_questions`. For each spec:
1. Show the `summary` and list any resolved questions
2. Ask: "Approve this spec, or request changes?"
3. If changes requested → edit the spec file directly and re-present
4. On approval → proceed to O6

#### Step O6: Update state.json

After user approves a spec, update that story's entry in `state.json`:

```json
{
  "stories": {
    "story-1-example": {
      "status": "code_spec",
      "code_spec_path": ".monkeymode/{feature-name}/code_specs/story-1-spec.md",
      "files_to_create": [
        "src/example/service.py",
        "tests/example/test_service.py"
      ],
      "files_to_modify": [
        "src/example/__init__.py"
      ],
      "last_updated": "2024-01-15T14:30:00Z"
    }
  }
}
```

**Only update the story's own entry** — do not modify other stories or top-level fields.

**Why `files_to_create` and `files_to_modify`?** Phase 4 uses these lists for conflict detection when running stories in parallel. Stories that share files cannot be in the same batch. These lists must be complete and accurate before Phase 4 begins.

#### Step O7: Complete Phase

Once all stories are approved and `state.json` is updated, confirm:
- Every story has `status: "code_spec"`
- Every story has `files_to_create` and `files_to_modify` populated (even if empty lists)

Then ask user: "All [N] code specs written and approved. Ready to move to Phase 4 (Implementation)?"

#### Step O8: Handle Failures

If a `code-spec-writer` subagent fails or does not produce a usable spec file:
1. Log the error in state
2. Fall back: write that story's spec directly as the orchestrator — read the design docs, decompose tasks, define function signatures and test cases following the same structure
3. Continue processing other stories normally

### Subagent Prompt Template

Each subagent receives a self-contained prompt with all necessary story-specific context.

```
## Your Story

**Story:** {story_title}
**Story ID:** {story_key}
**Feature:** {feature_name}

## User Story Text

{paste full user story text from user_stories.md}

## Acceptance Criteria

{paste all acceptance criteria for this story from 2b-acceptance.md}

## Output File

Write the completed spec to this exact path:
{workspace}/.monkeymode/{feature-name}/code_specs/{story-id}-spec.md

## Files to Read on Startup

Read ALL of these before writing the spec:

**Design context:**
- {workspace}/.monkeymode/{feature-name}/design/1a-discovery.md
- {workspace}/.monkeymode/{feature-name}/design/1b-contracts.md
- {workspace}/.monkeymode/{feature-name}/design/1c-operations.md
- {workspace}/.monkeymode/{feature-name}/stories/user_stories.md

**Language-specific coding guidelines (pick ONE):**
- Python: {skill_dir}/monkeymode/guides/PYTHON-CODING-GUIDELINES.md
- Java: {skill_dir}/monkeymode/guides/JAVA-CODING-GUIDELINES.md
- Angular: {skill_dir}/monkeymode/guides/ANGULAR-CODING-GUIDELINES.md
- .NET/C#: {skill_dir}/monkeymode/guides/DOTNET-CODING-GUIDELINES.md
- React: {skill_dir}/monkeymode/guides/REACT-CODING-GUIDELINES.md
- Terraform: {skill_dir}/monkeymode/guides/TERRAFORM-CODING-GUIDELINES.md

## Codebase References (Investigate These Before Planning)

Read these existing files to understand patterns you must follow:

{list of existing files to investigate — e.g. similar service, repository, test files.
 Be specific: include file paths identified from the Phase 1 design docs.}

## Conventions Seed (Pre-Filled by Orchestrator)

{Any conventions already confirmed in Phase 1, or "Discover from codebase references above."}

## Out of Scope

{Explicit out-of-scope items from the user story and design docs}
```

**CRITICAL:** Subagents write the spec file to the output path provided. They do NOT update `state.json` — the orchestrator updates state after user approval.

### Resuming Phase 3

If the session is interrupted during Phase 3:

1. **Read state.json** — check each story's status
2. Stories already `code_spec` → skip (spec written and approved)
3. Stories still `not_started` → re-run subagents for those stories
4. If a spec file exists on disk but state is not yet `code_spec` → re-present the written spec for user approval (skip re-running the subagent)
