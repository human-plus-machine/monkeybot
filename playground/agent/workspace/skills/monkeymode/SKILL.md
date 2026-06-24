---
name: monkeymode
description: MonkeyMode - Full Self Design, Develop, Deploy - Guides from feature idea to production through structured phases (Design → User Stories → Code Spec → Implementation → Verification → Integration). Creates state-tracked artifacts in your workspace for seamless continuation across sessions.
author: MonkeyMode Contributors
---

# MonkeyMode - Full Self Design, Develop, Deploy

## Intent

This skill orchestrates a complete feature development lifecycle through structured phases, with all artifacts saved in the user's workspace and state tracked for seamless continuation.

**User invokes:** `/monkeymode for [feature]`

**Agent guides through:**
1. **Phase 1: Design** - Create comprehensive technical design
2. **Phase 2: User Stories** - Decompose into parallelizable stories
3. **Phase 2B: Acceptance Checklist** - Draft the manual + automatable acceptance criteria
4. **Phase 3: Code Spec** - Detailed implementation plan per story
5. **Phase 4: Implementation** - Test-writer then implementer subagents (parallel)
6. **Phase 5: Verification** - Confirm implementation matches requirements
7. **Phase 6: Integration** - Wire stories together, shared files, e2e tests
8. **Phase 7: Acceptance** - Execute the acceptance checklist; feature marked complete only after all checks pass

## Workspace Setup

### On First Invocation

When `/monkeymode` is invoked, **ALWAYS**:

1. **Extract feature name** from user's request (convert to kebab-case)
2. **Check for state file:** Read `{workspace}/.monkeymode/{feature-name}/state.json`
3. **If state file doesn't exist:**
   - Ask about Q&A logging (see [Q&A Logging Setup](#qa-logging-setup))
   - Create `.monkeymode/{feature-name}/` directory in workspace
   - Create initial `state.json` with `current_phase: "1a"` and feature name
   - Start Phase 1A (Design Discovery)
4. **If state file exists:**
   - Read current phase and resume from there
   - Load context (feature name, selected story, etc.)

### Initial Preferences Setup

**On first invocation, before starting Phase 1, ask BOTH of the following questions together in a single message:**

#### Output Verbosity

Ask the user:
```
"Output verbosity level?

1. Full - Complete explanations, context, and reasoning (default)
2. Lite - Caveman mode: compressed output, drop filler, keep all technical detail"
```

If `verbosity` is `"full"`: communicate normally with complete explanations.
If `verbosity` is `"lite"`: apply caveman compression to ALL agent-to-user communication — drop articles, conjunctions, and filler words; use fragments; be telegraphic. Technical content (code, paths, specs, artifacts) is NEVER compressed. Only how the agent talks changes.

**Caveman rules (lite mode):**
- Drop: "I'll now proceed to...", "Great!", "Sure!", transition narration, hedge phrases
- Drop: articles (a/an/the), conjunctions where removable, pleasantries
- Keep: all technical facts, file paths, function names, decision rationale
- Keep: all user-facing questions (must still be clear)
- Format: fragments OK. "Phase 3 done. 6 specs. State updated. Phase 4?" not "Phase 3 is now complete..."

#### Q&A Logging

Ask the user:
```
"Save Q&A log?

1. Yes - Creates qa-log.md tracking all decisions (recommended for teams)
2. No - Skip logging"
```

If `save_qa_log` is `true`, create and maintain `qa-log.md` throughout the process.
If `save_qa_log` is `false`, skip all Q&A logging (do not create or update qa-log.md).

Store both preferences in state:
```json
{
  "context": {
    "verbosity": "full",
    "save_qa_log": true
  }
}
```

### State File Schema

The agent MUST create and maintain this file at `{workspace}/.monkeymode/{feature-name}/state.json`:

```json
{
  "feature_name": "string (kebab-case)",
  "current_phase": "1a",
  "phase_status": {
    "design_1a": "not_started|in_progress|completed",
    "design_1b": "not_started|in_progress|completed",
    "design_1c": "not_started|in_progress|completed",
    "user_stories": "not_started|in_progress|completed",
    "acceptance_checklist": "not_started|in_progress|completed",
    "code_spec": "not_started|in_progress|completed",
    "implementation": "not_started|in_progress|completed",
    "verification": "not_started|in_progress|completed",
    "integration": "not_started|in_progress|completed",
    "acceptance": "not_started|in_progress|completed"
  },
  "artifacts": {
    "design_docs": {
      "1a_discovery": ".monkeymode/{feature-name}/design/1a-discovery.md",
      "1b_contracts": ".monkeymode/{feature-name}/design/1b-contracts.md",
      "1c_operations": ".monkeymode/{feature-name}/design/1c-operations.md",
    },
    "user_stories_doc": ".monkeymode/{feature-name}/stories/user_stories.md",
    "acceptance_checklist": ".monkeymode/{feature-name}/stories/2b-acceptance.md",
    "qa_log": ".monkeymode/{feature-name}/qa-log.md"
  },
  "stories": {},
  "parallel_execution": {
    "enabled": false,
    "max_concurrent": 10,
    "batches": [],
    "current_batch": null
  },
  "rework_history": [],
  "integration": {
    "status": "not_started",
    "shared_files_merged": [],
    "integration_tests_added": 0,
    "completed_at": null
  },
  "acceptance": {
    "status": "not_started",
    "automated_passed": 0,
    "automated_failed": 0,
    "human_passed": 0,
    "human_failed": 0,
    "known_issues": [],
    "completed_at": null
  },
  "context": {
    "verbosity": "full",
    "save_qa_log": true
  },
  "last_updated": "ISO8601 timestamp"
}
```

### Per-Story State (Added After Phase 2)

After user stories are generated in Phase 2, the agent adds a `stories` object with one entry per story. This allows multiple developers to work on different stories in parallel without merge conflicts:

```json
{
  "stories": {
    "story-1-embeddings-component": {
      "title": "Embeddings Component",
      "status": "not_started|code_spec|tests_written|implementation|implementation_complete|verified|verification_failed|integrated|completed|failed",
      "code_spec_path": ".monkeymode/{feature-name}/code_specs/story-1-spec.md",
      "assigned_to": null,
      "current_task": null,
      "files_to_create": ["src/embeddings/interface.py", "src/embeddings/bedrock.py"],
      "files_to_modify": [],
      "blocked_by_rework": null,
      "verification": {
        "result": "pending|pass|fail|pass-with-warnings",
        "rework_attempts": 0,
        "rework_summary": null,
        "escalated_issues": [],
        "last_checked_at": null,
        "test_corrections": []
      },
      "last_updated": "ISO8601 timestamp"
    },
    "story-2-vector-store": {
      "title": "Vector Store Component",
      "status": "not_started|code_spec|tests_written|implementation|implementation_complete|verified|verification_failed|integrated|completed|failed",
      "code_spec_path": ".monkeymode/{feature-name}/code_specs/story-2-spec.md",
      "assigned_to": null,
      "current_task": null,
      "files_to_create": ["src/vector_store/interface.py", "src/vector_store/databricks.py"],
      "files_to_modify": [],
      "blocked_by_rework": null,
      "verification": {
        "result": "pending|pass|fail|pass-with-warnings",
        "rework_attempts": 0,
        "rework_summary": null,
        "escalated_issues": [],
        "last_checked_at": null,
        "test_corrections": []
      },
      "last_updated": "ISO8601 timestamp"
    }
  }
}
```

**Why per-story state?** When multiple developers or subagents work on different stories:
- Each story tracks its own file list for conflict detection
- No merge conflicts on `selected_story` or `current_phase`
- Clear visibility into which stories are in progress
- Supports true parallel development (human developers AND AI subagents)

### Workspace Artifact Structure

All generated files go in the **user's workspace** (NOT in the skills directory):

```
{workspace}/
├── .monkeymode/
│   └── {feature-name}/
│       ├── state.json              # State tracking (agent creates this)
│       ├── qa-log.md               # OPTIONAL: Q&A log (only if user opts in)
│       ├── design/
│       │   ├── 1a-discovery.md      # Phase 1A: Discovery & Core Design
│       │   ├── 1b-contracts.md      # Phase 1B: Detailed Contracts
│       │   ├── 1c-operations.md     # Phase 1C: Production Readiness
│       ├── stories/
│       │   ├── user_stories.md      # Phase 2A output
│       │   └── 2b-acceptance.md     # Phase 2B: Acceptance Checklist
│       ├── code_specs/
│       │   ├── story-1-spec.md      # Phase 3 output (per story)
│       │   └── story-2-spec.md
└── src/
    └── [actual production code]    # Phase 4: Code written here
```

### Parallel Execution State (Added in Phase 4)

When Phase 4 begins, the orchestrator adds a `parallel_execution` object to track batched subagent execution:

```json
{
  "parallel_execution": {
    "enabled": true,
    "max_concurrent": 10,
    "current_batch": {
      "batch_number": 1,
      "stories": ["story-1-embeddings-component", "story-2-vector-store", "story-3-storage"],
      "started_at": "ISO8601 timestamp",
      "status": "in_progress|completed|failed"
    },
    "batches": [
      {
        "batch_number": 1,
        "stories": ["story-1-embeddings-component", "story-2-vector-store"],
        "status": "completed",
        "started_at": "ISO8601 timestamp",
        "completed_at": "ISO8601 timestamp",
        "results": {
          "story-1-embeddings-component": "completed",
          "story-2-vector-store": "completed"
        }
      }
    ]
  }
}
```

**Why parallel execution state?**
- Tracks which stories have been batched together
- Records results per batch for auditability
- Enables resuming after interruptions (the orchestrator can read the last batch and continue)
- Provides a clear history of execution order

## Phase Flow & State Management

### Phase Detection Logic

**`current_phase` → `phase_status` mapping:**

| `current_phase` | `phase_status` key | Phase Guide |
|-----------------|-------------------|-------------|
| `"1a"` | `design_1a` | `phases/01a-design-discovery.md` |
| `"1b"` | `design_1b` | `phases/01b-design-contracts.md` |
| `"1c"` | `design_1c` | `phases/01c-design-operations.md` |
| `"2"` | `user_stories` | `phases/02a-user-stories.md` |
| `"2b"` | `acceptance_checklist` | `phases/02b-acceptance.md` |
| `"3"` | `code_spec` | `phases/03-code-spec.md` |
| `"4"` | `implementation` | `phases/04-implementation.md` |
| `"5"` | `verification` | `phases/05-verification.md` |
| `"6"` | `integration` | `phases/06-integration.md` |
| `"7"` | `acceptance` | `phases/07-acceptance.md` |
| `"completed"` | — | Feature complete |

```
1. Extract feature name from user's request (convert to kebab-case)
2. Read {workspace}/.monkeymode/{feature-name}/state.json
3. If file doesn't exist:
   → Create .monkeymode/{feature-name}/ directory
   → Create state.json with current_phase: "1a"
   → Start Phase 1A
4. If file exists:
   → Read current_phase field (see mapping table above)
   → If "completed": Announce feature is done, ask if user wants to revisit or start a new feature
   → Otherwise: Resume from that phase/sub-phase, load context for continuity
```

### Phase Transitions

**CRITICAL: Never auto-advance phases. Always ask user for confirmation.**

After completing work in a phase:
1. Save the artifact to workspace
2. Update state.json with completed status
3. **Ask user:** "Phase [N] complete. Ready to move to Phase [N+1]?"
4. If yes → Update state.json current_phase, start next phase
5. If no → Keep in current phase for refinements

**Approval is per-phase, never cumulative.** When the user says "yes", "proceed", "let's go", "continue with next steps", or any similar affirmation, this ONLY grants approval to advance to the **immediately next phase** — never beyond. After completing that next phase, you MUST stop and ask for approval again before advancing further. No user message — regardless of phrasing — should be interpreted as blanket approval to skip future phase-transition checkpoints.

## Phase Reference Guides

The agent should read these files from the skills directory for detailed methodology:

- **Phase 1A:** Read `phases/01a-design-discovery.md` - Discovery, Architecture, Core Data Model
- **Phase 1B:** Read `phases/01b-design-contracts.md` - API Contracts, Integration, Testing
- **Phase 1C:** Read `phases/01c-design-operations.md` - Security, Performance, Deployment, Observability, Risk
- **Phase 2:** Read `phases/02a-user-stories.md` - Story decomposition methodology
- **Phase 2B:** Read `phases/02b-acceptance.md` - Draft acceptance checklist (curl commands, CLI checks, UI steps) from the approved stories and design docs
- **Phase 3:** Read `phases/03-code-spec.md` - Code spec creation methodology. **Important:** Create code specs for all stories before moving to Phase 4 (the orchestrator needs complete file lists for conflict detection).
- **Phase 4:** Read `phases/04-implementation.md` - Implementation methodology (two-step: test-writer then implementer subagents, parallel)
- **Phase 5:** Read `phases/05-verification.md` - Verification of implementation against requirements
- **Phase 6:** Read `phases/06-integration.md` - Cross-story integration and wiring
- **Phase 7:** Read `phases/07-acceptance.md` - Execute acceptance checklist; agent runs automatable checks, human confirms UI items; feature marked complete after all checks pass
- **Rework:** Read `phases/rework.md` - Structured rework when feedback, bugs, or changed requirements require revisiting previous phases

### When to Load the Rework Guide

The agent should read `phases/rework.md` when ANY of these occur:

**Automatic triggers (orchestrator handles in Phase 5):**
- Verifier subagent returns status "fail" or "pass-with-warnings" for any story
- Reworker subagent reports "escalated" status (spec/design-level issue that needs rework guide)
- Rework cycle count exceeds 3 attempts for a single story

**User-initiated triggers:**
- User says "change", "rework", "redo", "fix", "update", or "revise" about a **completed** artifact
- Code review feedback requires changes beyond the current implementation task
- Integration between stories fails due to contract mismatches
- New requirements emerge that affect completed phases

### Language-Specific Coding Guidelines

The `guides/` directory contains coding guidelines for specific programming languages. The agent should load the appropriate guide **before starting Phase 4 (Implementation)** based on the project's primary language:

- **Python:** Read `guides/PYTHON-CODING-GUIDELINES.md` - Enterprise-grade Python standards (style, type hints, architecture, testing, security, performance)
- **Java:** Read `guides/JAVA-CODING-GUIDELINES.md` - Enterprise-grade Java standards (Google Java Style, generics, Javadoc, Spring DI, JUnit 5, security, performance)
- **Angular:** Read `guides/ANGULAR-CODING-GUIDELINES.md` - Enterprise-grade Angular/TypeScript standards (component patterns, signals, strict mode, testing, security, performance)
- **.NET / C#:** Read `guides/DOTNET-CODING-GUIDELINES.md` - Enterprise-grade .NET/C# standards (Microsoft conventions, nullable types, async/await, xUnit, EF Core, security)
- **React:** Read `guides/REACT-CODING-GUIDELINES.md` - Enterprise-grade React/TypeScript standards (Server Components, hooks, React Compiler, testing, security, performance)
- **Terraform:** Read `guides/TERRAFORM-CODING-GUIDELINES.md` - Enterprise-grade Terraform/IaC standards (HCL style, module structure, validation, state management, security, testing)

Additionally, `guides/IMPLEMENTATION-PATTERNS.md` contains language-agnostic reference examples for testing, error handling, logging, and common architecture patterns. Subagents reference this file during Phase 4.

When implementing code, follow the loaded language guidelines for all code style, architecture, testing, and quality decisions. If no guideline exists for the project's language, follow established conventions found in the existing codebase.

## Resuming Work

If user invokes `/monkeymode` in a workspace with existing state:

1. **Extract feature name** from user's request
2. **Read state file:** `{workspace}/.monkeymode/{feature-name}/state.json`
3. **Announce context:** "Resuming MonkeyMode for '{feature_name}'. Currently in Phase {N}: {phase_name}."
4. **Load artifacts:** Read relevant files from workspace
5. **Continue from current phase**

**Note:** If user doesn't specify feature name, list available features:
```
User: "/monkeymode"
Agent: "Found existing MonkeyMode projects in this workspace:
        1. favorites-feature (Phase 3: Code Spec)
        2. notifications-system (Phase 2: User Stories)
        
        Which feature would you like to continue with?"
```

## Agent Instructions Summary

### On Every Invocation

1. **Extract feature name** from user's request (or list available if not specified)
2. **Read workspace state:** `{workspace}/.monkeymode/{feature-name}/state.json`
3. **Determine phase:** Extract current_phase or start at 1a
4. **Load phase guide:** Read appropriate `phases/{N}-*.md` file
5. **Load workspace artifacts:** Read relevant design/stories/specs
6. **Execute phase:** Follow methodology from phase guide
7. **Save artifacts:** Write to `{workspace}/.monkeymode/{feature-name}/...`
8. **Update Q&A log (if enabled):** If `context.save_qa_log` is `true`, append Q&A to `qa-log.md`
9. **Update state:** Write updated `{workspace}/.monkeymode/{feature-name}/state.json`
10. **Ask for confirmation:** Before advancing to next phase

### Phase Flow (Phases 4 → 5 → 6)

The orchestrator spawns parallel subagents for implementation, verification, and integration.

**Each phase transition requires explicit user confirmation:**
- After Phase 4 completes → Stop and ask before starting Phase 5
- After Phase 5 completes → Stop and ask before starting Phase 6
- Parallel subagent execution is scoped *within* a phase — transitions *between* phases always require user approval.

**Pipeline:**
```
Phase 2B: Acceptance Checklist  →  agent drafts checklist (curl, CLI, UI steps); user approves
       ↓ (ask user to proceed)
Phase 3: Code Spec
  Step 1 →  code-spec-writer subagents (parallel, all stories at once, max 10)
            ↓ collect draft specs + open_questions per story
  Step 2 →  orchestrator resolves open questions with user, presents each spec for approval
            ↓ orchestrator writes approved specs to disk + updates state.json
       ↓ (ask user to proceed)
Phase 4: Implementation
  Step 1 →  test-writer subagents (parallel, per batch) — writes red tests from code spec
            ↓ confirm all tests red
  Step 2 →  implementer subagents (parallel, per batch) — makes tests pass; logs any test corrections
       ↓ (ask user to proceed)
Phase 5: Verification     →  verifier subagents (parallel, read-only)
            ↓                      ↓ includes test correction audit
            ↓ fail                 ↓ pass
         reworker         →  re-verify (loop, max 3 attempts)
       ↓ (ask user to proceed)
Phase 6: Integration      →  orchestrator merges shared files, writes e2e tests
       ↓ (ask user to proceed)
Phase 7: Acceptance       →  agent runs automatable checks; human confirms UI items
            ↓ all pass            ↓ failures found
         completed          →  rework loop → re-run failed checks
```

### Phase 3 Orchestrator Responsibilities

**FIRST read `phases/03-code-spec.md`** for the full orchestration workflow before executing this phase. The spec-writing methodology (task decomposition, test case tables, output format, quality checklist) is built into the `code-spec-writer` subagent type and used by the subagent directly.

**The orchestrator MUST spawn `code-spec-writer` subagents — it must NOT write code specs itself.** The code-spec-writer subagent does the codebase investigation and produces a structured draft; the orchestrator resolves questions, presents specs for approval, and commits artifacts.

Phase 3 runs as a **two-step pipeline across all stories**:

Key responsibilities:

1. **Collect all stories** — Read `state.json` and identify all stories that need code specs (status `not_started` or `code_spec` incomplete). This is typically all stories from Phase 2.
2. **Build subagent prompts** — For each story, assemble a self-contained prompt containing: full user story text, acceptance criteria, design doc excerpts, the exact output file path to write, codebase references to investigate, language guidelines path, and any conventions already known from Phase 1.
3. **Step 1 — Spawn code-spec-writer subagents** — Launch one `code-spec-writer` subagent per story (`subagent_type: "code-spec-writer"`). Each subagent writes its spec file directly to the path provided in the prompt. **Launch all subagents in a single message (parallel tool calls). Max 10 concurrent subagents.** If there are more than 10 stories, batch them (10 per batch), completing each batch before spawning the next.
4. **Step 2 — Review written specs** — For each subagent result, read the **Structured Output JSON** returned directly: `spec_ready`, `summary`, `files_to_create`, `files_to_modify`, `open_questions`
   - If `spec_ready: false` → surface blocking `open_questions` to the user, apply answers to the spec file, then present the `summary` for approval
   - If `spec_ready: true` → present the `summary` to the user for approval (no need to re-read the file)
5. **Update state.json on approval** — After user approves each spec, update that story's entry: `status: "code_spec"`, `code_spec_path`, `files_to_create`, `files_to_modify`
6. **Present specs sequentially** — Even though writing is parallel, present specs to the user one at a time for review and approval before advancing
7. **Handle failures** — If a subagent fails or produces no file, log the error in state and fall back to writing that story's spec directly as the orchestrator
8. **Complete phase** — Once all stories have approved specs and state is updated, ask user to proceed to Phase 4

**Subagent prompt template for code-spec-writer:**

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

**Language-specific coding guidelines:**
- {skill_dir}/monkeymode/guides/{LANGUAGE}-CODING-GUIDELINES.md

## Codebase References (Read These to Discover Existing Patterns)

{list of existing files to investigate — e.g. similar service, repository, test files.
 Be specific: include file paths the orchestrator identified from Phase 1 design docs.}

## Conventions Seed (Pre-Filled by Orchestrator)

{Any conventions already confirmed in Phase 1, or "Discover from codebase references above."}

## Out of Scope

{Explicit out-of-scope items from the user story and design docs}

## Communication Style

verbosity: {verbosity}

When reporting results, open_questions, and findings back:
- full: normal prose
- lite: caveman compression — fragments, no filler, drop articles. Technical content (code, paths, names) exact as always.
```

**CRITICAL RULES for code-spec-writer subagents:**
- Launch all subagents for a batch in a **single message** (parallel tool calls)
- Never exceed 10 concurrent subagents
- Each subagent gets a **complete, self-contained prompt** — subagents have NO access to the conversation history
- **Do NOT pass a `model` parameter** — omit it so subagents inherit the parent's model
- Subagents write the spec file directly to the output path provided — they do NOT update `state.json`; the orchestrator updates state after user approval

**After Phase 3 completes:**
- Every story must have `status: "code_spec"` and a populated `code_spec_path` in `state.json`
- Every story must have `files_to_create` and `files_to_modify` lists populated (required for Phase 4 conflict detection)

---

### Phase 4 Orchestrator Responsibilities

**FIRST read `phases/04-implementation.md`** for the full workflow before executing this phase. The phase guide contains the batching algorithm, conflict detection rules, subagent prompt templates, and state update procedures that are essential for correct orchestration.

Phase 4 runs as a **two-step pipeline per batch**: test-writer subagents first, then implementer subagents.

Key responsibilities:

1. **Analyze stories** — Identify which stories have completed code specs and are ready for implementation
2. **Detect file conflicts** — Check `files_to_create` and `files_to_modify` across stories; stories that share files CANNOT run in the same batch
3. **Batch stories** — Group up to 10 conflict-free stories per batch
4. **Step 1 — Spawn test-writer subagents** — Launch one `test-writer` subagent per story (`subagent_type: "test-writer"`). **Do NOT pass a `model` parameter.**
5. **Confirm red state** — After test-writers complete, verify all new tests fail. Do not proceed until confirmed. Update state to `tests_written` per story.
6. **Step 2 — Spawn implementer subagents** — Launch one `implementer` subagent per story (`subagent_type: "implementer"`). Include the list of test files already written in the prompt. **Do NOT pass a `model` parameter.** Never use `model: "fast"`.
7. **Collect test corrections** — After implementers complete, aggregate any Option B escape hatch corrections from their reports into `state.json` under `verification.test_corrections` per story
8. **Own state.json** — Only the orchestrator writes to `state.json`; subagents do NOT touch it
9. **Reconcile results** — After each batch completes, run full test suite, update state
10. **Handle failures** — If a subagent fails, mark the story as `failed`, log the error, and continue with remaining stories
11. **Report to user** — Summarize both passes (test-writer and implementer) and any test corrections before launching the next batch

### Phase 5 Orchestrator Responsibilities

**FIRST read `phases/05-verification.md`** for the full workflow before executing this phase.

**The orchestrator MUST spawn `verifier` subagents for verification — it must NOT verify stories itself.** The orchestrator lacks the structured verification checklist that the verifier subagent follows. Even if the orchestrator has seen all implementation results during Phase 4, it must still delegate verification to `verifier` subagents.

Key responsibilities:

1. **Spawn verifiers** — Launch one `verifier` subagent per implemented story (`subagent_type: "verifier"`, parallel, read-only)
2. **Triage results** — For each story:
   - If verifier reports `pass` → Story moves to Phase 6 (integration), update state: `status: verified`, `verification.result: pass`
   - If verifier reports `pass-with-warnings` → Present warnings to user. If user proceeds: update state `status: verified`, `verification.result: pass-with-warnings`. If user wants fixes: enter rework loop, update state `status: verification_failed`, `verification.result: pass-with-warnings`
   - If verifier reports `fail` → Launch rework loop, update state: `status: verification_failed`, `verification.result: fail`
3. **Rework loop** — For failed/warned stories:
   - Increment `verification.rework_attempts` counter
   - Spawn `reworker` subagent (`subagent_type: "reworker"`) with the verification report and failed story details
   - If reworker reports `status: completed` → Re-verify the story with a `verifier` subagent
   - If reworker reports `status: escalated` → STOP rework loop and load `phases/rework.md` for spec/design-level issues
   - If verification.rework_attempts >= 3 → STOP rework loop and report to user: "Story {name} exceeded 3 rework attempts. Needs design/spec review."
4. **Repeat verification** — After each reworker fix, re-run `verifier` until:
   - Verification passes (status: `pass`) → move to Phase 6
   - Rework escalated → load rework guide
   - Max attempts reached → report to user
5. **Report to user** — After all verifications complete, summarize:
   - Stories that passed verification (ready for Phase 6)
   - Stories that escalated (waiting for rework guide analysis)
   - Stories that exceeded rework cycles (need user intervention)

### Phase 6 Orchestrator Responsibilities

**FIRST read `phases/06-integration.md`** for the full workflow before executing this phase. The phase guide contains the shared file merge strategy, cross-story wiring steps, and integration test patterns.

Key responsibilities:

1. **Identify integration points** — Shared files, cross-story imports, DI wiring, config entries
2. **Merge shared files** — Apply changes from all stories to shared files in order
3. **Write integration tests** — End-to-end tests across story boundaries
4. **Run full verification** — All tests pass, linter clean, no regressions
5. **Report to user** — Summarize integration results, advance to Phase 7

### Phase 7 Orchestrator Responsibilities

**FIRST read `phases/07-acceptance.md`** for the full workflow before executing this phase.

Key responsibilities:

1. **Load checklist** — Read `stories/2b-acceptance.md`; if it doesn't exist, stop and tell the user Phase 2B must be run first
2. **Run automated checks** — Execute all `agent-automatable` items (curl, CLI) exactly as written; record PASS/FAIL per item
3. **Report automated results** — If any fail, offer to investigate before proceeding to manual checks
4. **Present human checks** — Walk through `human-ui` and `human-verify` items one by one; for `human-verify` items, run the agent-side setup first
5. **Triage failures** — Classify as implementation bug, design gap, or environment issue; load `phases/rework.md` for bugs and gaps
6. **Mark complete** — Update state to `completed` only after all items pass (or user accepts known issues)

### Never Do

- ❌ Auto-advance phases without user confirmation
- ❌ Interpret "proceed", "continue", "next steps", or any affirmation as approval for more than one phase transition — approval is always for the immediately next phase only
- ❌ Save artifacts to skills directory
- ❌ Skip state updates
- ❌ Assume phase without reading state
- ❌ Create artifacts without proper workspace paths
- ❌ Forget to load context when resuming
- ❌ Fix symptoms without tracing to the origin phase (see Rework guide)
- ❌ Make invisible rework — always track changes in state.json
- ❌ Let subagents write to `state.json` — only the orchestrator updates state
- ❌ Write code specs directly in Phase 3 — always spawn `code-spec-writer` subagents and let them investigate the codebase
- ❌ Update `state.json` for a story before the user approves its spec
- ❌ Advance to Phase 4 before every story has `files_to_create` and `files_to_modify` populated in `state.json`
- ❌ Verify stories without spawning `verifier` subagents — the orchestrator must always delegate verification
- ❌ Run stories with shared files in the same parallel batch
- ❌ Launch more than 10 subagents concurrently

### Always Do

- ✅ Extract feature name first
- ✅ Read state from `.monkeymode/{feature-name}/state.json`
- ✅ Save all artifacts to workspace
- ✅ Update state after significant actions
- ✅ Log all Q&A exchanges to `qa-log.md` immediately (if enabled)
- ✅ Ask user before phase transitions
- ✅ Load phase guides for detailed methodology
- ✅ Use workspace-relative paths for all artifacts
- ✅ When rework is needed, load `phases/rework.md` and follow the structured process
- ✅ Trace issues to their origin phase before fixing
- ✅ In Phase 3: Spawn one `code-spec-writer` subagent per story (all in parallel, max 10 at once)
- ✅ In Phase 3: Build each subagent prompt with full story text, AC, design doc excerpts, the exact output file path, and specific codebase file references
- ✅ In Phase 3: Resolve all blocking open_questions with the user before approving a spec
- ✅ In Phase 3: Present each spec to the user for approval using the subagent's returned summary — no need to re-read from disk
- ✅ In Phase 3: Populate `files_to_create` and `files_to_modify` in `state.json` for every story before Phase 4
- ✅ In Phase 4: Load language-specific coding guidelines from `guides/` before writing any code
- ✅ In Phase 4: Check for file conflicts before batching stories for parallel execution
- ✅ In Phase 4: Run full test suite after each batch completes
- ✅ In Phase 4: Report batch results to user before proceeding to next batch
- ✅ In Phase 5: Verify every story against its code spec before integration
- ✅ In Phase 5: Use `reworker` subagents for implementation fixes, escalate spec issues to `phases/rework.md`
- ✅ In Phase 6: Merge shared files, wire cross-story dependencies, write integration tests
- ✅ In Phase 7: Run all `agent-automatable` checks before presenting human checks
- ✅ In Phase 7: Classify failures (implementation bug vs design gap vs environment) before reworking
- ✅ In Phase 7: Mark feature `completed` only after every acceptance item is confirmed

## Quality Standards

Every phase output must meet quality standards defined in phase guides:
- **Design:** Top 1% quality - performance, scalability, security
- **User Stories:** Zero dependencies in Sprint 1, fully parallelizable
- **Code Spec:** Atomic tasks, complete signatures, test specifications — written to disk by `code-spec-writer` subagents, approved by user before state.json is updated
- **Implementation:** Production-ready, tested, follows existing patterns, no cross-story file conflicts
- **Verification:** Every acceptance criterion confirmed, signatures match spec, full test coverage
- **Integration:** Shared files merged cleanly, cross-story contracts verified, e2e tests passing
- **Acceptance:** All automatable checks pass, all human checks confirmed, feature marked completed
