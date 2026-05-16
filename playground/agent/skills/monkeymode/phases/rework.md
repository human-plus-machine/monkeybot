---
name: rework
description: Rework & Iteration (cross-cutting, not a numbered phase). Guides structured rework when feedback, bugs, or changed requirements require revisiting previous phase artifacts. Routes changes to the correct origin phase and cascades updates downstream.
---

# Rework & Iteration

## Purpose

Provide a structured process for handling rework — when feedback, bugs, design flaws, or changed requirements require revisiting previous phase artifacts. Without this, rework is ad-hoc and either too aggressive (redo everything) or too timid (patch symptoms, not causes).

**This is not a sequential phase.** Rework can be triggered at any point and sends you back to the correct origin phase with targeted changes, then cascades updates forward through affected downstream artifacts.

## When to Use This Guide

The agent should read this guide when any of the following occur:

- User says "change", "rework", "redo", "fix", "update", "revise" about a completed artifact
- Code review feedback requires changes beyond the current implementation
- Implementation reveals a flaw in the design or spec
- Integration between stories fails due to contract mismatches
- QA/testing discovers issues that trace back to design decisions
- Stakeholder feedback invalidates previous assumptions
- New requirements emerge mid-process that affect completed phases

## Rework Principles

### 1. Fix the Root, Not the Symptom

```
❌ BAD: Bug in implementation → patch the code
   (But the spec said to do it this way, so next developer repeats the bug)

✅ GOOD: Bug in implementation → trace to origin → fix spec + code
   (Root cause fixed, artifacts stay consistent)
```

### 2. Minimal Blast Radius

```
Don't redo everything. Only update what actually changed.

❌ BAD: "The API contract changed, let me redo the entire design document"
✅ GOOD: "The API contract changed, let me update 1b-contracts.md section 3.2,
         then update the affected code spec tasks, then fix the implementation"
```

### 3. Cascade Forward, Never Skip

```
If you change Phase 1, check Phase 2, 3, 4.
If you change Phase 2, check Phase 3, 4.
If you change Phase 3, check Phase 4.
If you change Phase 4 only, no cascade needed.

Never update a downstream artifact without checking if its upstream is still valid.
```

### 4. Track Everything

```
Every rework cycle must be tracked in state.json.
This creates an audit trail and prevents rework loops.
```

## Rework Classification

### Step 1: Identify the Trigger

Before making any changes, classify what happened:

| Trigger | Description | Example |
|---------|-------------|---------|
| `feedback-design` | Stakeholder/reviewer wants design changes | "Use event sourcing instead of CRUD" |
| `feedback-stories` | Story decomposition needs adjustment | "These stories aren't independent enough" |
| `feedback-spec` | Code spec needs revision | "The function signatures don't handle pagination" |
| `feedback-code` | Code review feedback on implementation | "This doesn't follow our error handling pattern" |
| `bug-implementation` | Bug found in implemented code | "The pagination returns duplicates" |
| `bug-spec` | Implementation correct per spec, but spec was wrong | "Spec says return 404, should be 409 for duplicates" |
| `bug-design` | Design flaw discovered during implementation | "The data model can't support the required query pattern" |
| `integration-failure` | Stories don't integrate correctly | "Story 1's interface doesn't match Story 2's usage" |
| `requirements-change` | New or changed requirements | "We also need soft-delete support" |
| `performance-issue` | Performance doesn't meet targets | "Query takes 2s, target is 200ms — need index strategy change" |

### Step 2: Determine the Origin Phase

**Critical: Find where the actual change needs to happen, not where the symptom appeared.**

```
Symptom in Phase 4 (Implementation)?
  → Is the code wrong but spec is right?           → Origin: Phase 4
  → Is the code correct but spec is wrong?          → Origin: Phase 3
  → Is the spec correct but stories are wrong?      → Origin: Phase 2
  → Are stories correct but design is wrong?        → Origin: Phase 1

Ask yourself: "If I fix ONLY this phase, will the problem be fully resolved?"
  → If yes, this is the origin phase
  → If no, go one phase earlier and ask again
```

**Decision Tree:**

```
User reports issue
    │
    ├─ Is this a code-only fix? (typo, missing null check, wrong variable)
    │   └─ YES → Origin: Phase 4 (Implementation) — fix code, no cascade
    │
    ├─ Is the spec wrong? (wrong signature, missing error case, bad algorithm)
    │   └─ YES → Origin: Phase 3 (Code Spec) — fix spec, cascade to Phase 4
    │
    ├─ Are the stories wrong? (wrong decomposition, dependencies, missing story)
    │   └─ YES → Origin: Phase 2 (User Stories) — fix stories, cascade to Phase 3+4
    │
    └─ Is the design wrong? (wrong architecture, bad data model, missing integration)
        └─ YES → Origin: Phase 1 (Design) — fix design, cascade to Phase 2+3+4
```

### Step 3: Assess the Scope

| Scope | Description | Estimated Effort | Examples |
|-------|-------------|------------------|----------|
| **Cosmetic** | Naming, formatting, typos, comment updates | Minutes | Rename a field, fix a docstring |
| **Targeted** | Single function, single endpoint, single test | < 1 hour | Add missing error case, fix return type |
| **Moderate** | Multiple related functions, one component | 1-4 hours | Redesign a service method + its tests |
| **Significant** | Multiple components, cross-story impact | 4-8 hours | Change data model affecting multiple stories |
| **Major** | Architectural change, fundamental approach shift | 1-3 days | Switch from REST to event-driven |

**For Major rework, ALWAYS confirm with the user before proceeding:**
```
"This change affects the fundamental architecture. Here's what needs to change:
- Phase 1A: [specific changes]
- Phase 1B: [specific changes]
- Phase 2: [N stories affected]
- Phase 3: [N specs need updating]
- Phase 4: [N files need rewriting]

Estimated effort: [time]. Should I proceed?"
```

## Rework Process

### Step 1: Document the Rework Request

Before making any changes, create a rework entry:

```markdown
## Rework: [Brief Description]

**Trigger:** [trigger type from classification table]
**Origin Phase:** [1a/1b/1c/2/3/4]
**Scope:** [cosmetic/targeted/moderate/significant/major]
**Requested By:** [user/code-review/qa/integration-test]
**Description:** [What needs to change and why]

**Affected Artifacts:**
- [ ] `.monkeymode/{feature}/design/1a-discovery.md` — [what changes]
- [ ] `.monkeymode/{feature}/design/1b-contracts.md` — [what changes]
- [ ] `.monkeymode/{feature}/stories/user_stories.md` — [what changes]
- [ ] `.monkeymode/{feature}/code_specs/story-N-spec.md` — [what changes]
- [ ] `src/[path]` — [what changes]

**Impact on Other Stories:** [None / Story N affected because...]
```

### Step 2: Fix the Origin Phase Artifact

Go to the origin phase and make the targeted change.

**Rules for editing existing artifacts:**

1. **Mark changes clearly** — Add a revision section at the top of the artifact:
```markdown
## Revision History
| Date | Change | Reason | Rework ID |
|------|--------|--------|-----------|
| YYYY-MM-DD | Changed data model field X to Y | Performance requirement | rework-001 |
```

2. **Don't delete, annotate** — When removing a design decision, explain why:
```markdown
~~### Approach A: Redis Caching~~
**[REVISED in rework-001]:** Replaced with in-memory caching due to infrastructure constraints. See updated section below.

### Approach A (Revised): In-Memory Caching
[Updated content]
```

3. **Preserve context** — Future readers need to understand why things changed

### Step 3: Cascade Forward

After fixing the origin, cascade changes to every downstream phase:

#### Cascade from Phase 1 (Design)

```
Phase 1 changed
    │
    ├─→ Phase 2 (User Stories)
    │   ├─ Do any stories need new/changed acceptance criteria?
    │   ├─ Do story boundaries need adjustment?
    │   ├─ Are any new stories needed?
    │   └─ Do integration contracts need updating?
    │
    ├─→ Phase 3 (Code Specs) — for each affected story
    │   ├─ Do function signatures need updating?
    │   ├─ Do data structures need updating?
    │   ├─ Do test cases need updating?
    │   └─ Are new tasks needed or old tasks removed?
    │
    └─→ Phase 4 (Implementation) — for each affected spec
        ├─ Does existing code need modification?
        ├─ Do tests need updating?
        └─ Is new code needed?
```

#### Cascade from Phase 2 (User Stories)

```
Phase 2 changed
    │
    ├─→ Phase 3 (Code Specs) — for each affected story
    │   ├─ Update technical context section
    │   ├─ Update acceptance criteria mapping
    │   └─ Adjust task breakdown if scope changed
    │
    └─→ Phase 4 (Implementation) — for each affected spec
        ├─ Update code to match new spec
        └─ Update/add tests
```

#### Cascade from Phase 3 (Code Spec)

```
Phase 3 changed
    │
    └─→ Phase 4 (Implementation)
        ├─ Update implementation to match new spec
        ├─ Update tests
        └─ Re-run verification
```

#### No Cascade Needed

```
Phase 4 changed (code-only fix)
    │
    └─ Fix code + tests, commit, done
```

### Step 4: Verify Consistency

After cascading all changes, verify the entire chain is consistent:

```markdown
### Consistency Check

- [ ] Design document reflects the current intended architecture
- [ ] User stories match the design (acceptance criteria, technical context)
- [ ] Code specs match the stories (tasks cover all acceptance criteria)
- [ ] Implementation matches the specs (code follows function signatures)
- [ ] Tests match the implementation (all cases covered)
- [ ] No orphaned references (nothing points to deleted/changed artifacts)
```

### Step 5: Update State

Update `state.json` to track the rework:

```json
{
  "rework_history": [
    {
      "id": "rework-001",
      "trigger": "feedback-code",
      "origin_phase": "3",
      "scope": "targeted",
      "description": "Add missing pagination error handling",
      "affected_artifacts": [
        ".monkeymode/{feature}/code_specs/story-1-spec.md",
        "src/favorites/service.py",
        "tests/favorites/test_service.py"
      ],
      "affected_stories": ["story-1-favorites-api"],
      "status": "completed",
      "created_at": "2024-01-20T10:00:00Z",
      "completed_at": "2024-01-20T11:30:00Z"
    }
  ]
}
```

**Rework status values:**
- `identified` — Rework documented, not yet started
- `in_progress` — Actively making changes
- `cascading` — Origin fixed, cascading to downstream artifacts
- `verifying` — Changes complete, running consistency check
- `completed` — All changes made and verified

## Common Rework Scenarios

### Scenario 1: Code Review Feedback

**Trigger:** Reviewer says "use the existing error handler pattern, not a custom one"

```
1. Classify: feedback-code, Origin: Phase 4, Scope: targeted
2. Check: Is this just a code pattern issue or does the spec specify the wrong pattern?
   → Spec says "use custom error handler" → Origin is actually Phase 3
   → Spec doesn't specify → Origin stays Phase 4
3. Fix origin, cascade if needed
4. Commit: "fix(favorites): use existing error handler pattern per code review [rework-001]"
```

### Scenario 2: Integration Failure

**Trigger:** Story 1 and Story 2 don't integrate — interface mismatch

```
1. Classify: integration-failure, Origin: Phase 2 (contracts defined there), Scope: moderate
2. Document: Which interface, which methods, what's the mismatch
3. Fix Phase 2: Update integration contracts in stories/user_stories.md
4. Cascade to Phase 3: Update affected code specs for both stories
5. Cascade to Phase 4: Update implementations for both stories
6. Verify: Run integration tests to confirm fix
```

### Scenario 3: Performance Issue

**Trigger:** Query takes 2s, target is 200ms from Phase 1C

```
1. Classify: performance-issue, determine origin:
   → Is the query unoptimized? → Origin: Phase 4 (add index, optimize)
   → Is the data model wrong for this query? → Origin: Phase 1A (data model change)
   → Is the caching strategy insufficient? → Origin: Phase 1C (performance strategy)
2. Fix origin, cascade
3. For data model changes: affects Phase 1B (contracts), Phase 2 (stories may need
   migration task), Phase 3 (spec changes), Phase 4 (implementation)
```

### Scenario 4: New Requirement Mid-Implementation

**Trigger:** "We also need to support bulk favorites import"

```
1. Classify: requirements-change, Origin: Phase 1A (new capability), Scope: significant
2. STOP: Confirm with user before proceeding
   "This new requirement affects the design. I recommend:
    Option A: Add to current feature scope (rework design through implementation)
    Option B: Create a separate MonkeyMode feature for bulk import
    Option C: Add as Sprint 2/3 story in current feature
    Which approach do you prefer?"
3. Based on user choice, either rework or defer
```

### Scenario 5: Story-Level Rework

**Trigger:** A single story's implementation is rejected — needs different approach

```
1. Classify: feedback-spec or feedback-code, Origin: Phase 3 or 4
2. Only affects the single story — other stories are untouched
3. Update that story's state to reflect rework:
   {
     "stories": {
       "story-1-favorites-api": {
         "status": "code_spec",  ← Regressed from "implementation"
         "current_task": "Rework: [description]"
       }
     }
   }
   Also add an entry to the top-level rework_history array (see Step 5 of the Rework Process).
4. Fix spec, then re-implement affected tasks only
```

## Rework Anti-Patterns

❌ **Scorched Earth Rework**
```
"Something's wrong with the API, let me redo the entire design from Phase 1A"
→ Trace to the actual origin. If only the API contract is wrong, fix Phase 1B only.
```

❌ **Symptom Patching**
```
"The implementation has a bug, let me just fix the code"
→ Ask: Is the bug because the code is wrong, or because the spec is wrong?
   If the spec is wrong, fix the spec FIRST, then fix the code.
```

❌ **Invisible Rework**
```
"I'll just quietly update this file"
→ All rework must be tracked in state.json and annotated in artifacts.
   Future developers/agents need to understand what changed and why.
```

❌ **Cascading Without Checking**
```
"Design changed, so let me blindly regenerate all user stories"
→ Check each story: does this design change actually affect it?
   Often only 1-2 stories are affected, not all of them.
```

❌ **Rework Loops**
```
Fix A causes issue B, fix B causes issue C, fix C causes issue A again
→ If you're on rework-003+ for the same area, STOP.
   Step back, re-evaluate the fundamental approach.
   Consider: "Is the architecture fundamentally wrong for this requirement?"
```

❌ **Deferring Rework**
```
"I'll fix the spec later, let me just implement the fix"
→ Artifacts drift out of sync. Next developer reads stale spec, repeats the bug.
   Fix artifacts NOW, even if it takes longer.
```

## Rework in Parallel Development

When multiple developers are working on different stories and rework is needed:

### Rework Affects Only One Story
- Developer fixes their own story's spec + implementation
- No coordination needed
- Other developers are unaffected

### Rework Affects Integration Contracts
- **STOP all affected developers**
- Fix the contract in `stories/user_stories.md` first
- Communicate the change to all affected developers
- Each developer updates their own story's spec and implementation
- Add a note in each affected story's state: `"blocked_by_rework": "rework-001"`

### Rework Affects Design (Phase 1)
- **STOP all developers**
- Fix the design
- Cascade to stories — identify which are affected
- Only affected stories need rework
- Unaffected stories continue normally
- Use state.json to track which stories are blocked:
```json
{
  "stories": {
    "story-1-api": {
      "status": "implementation",
      "blocked_by_rework": null
    },
    "story-2-data-model": {
      "status": "code_spec",
      "blocked_by_rework": "rework-002"
    }
  }
}
```

## Output: Rework Summary

After completing a rework cycle, document the summary:

```markdown
## Rework Summary: [rework-id]

**Trigger:** [What happened]
**Origin Phase:** [Phase number]
**Scope:** [cosmetic/targeted/moderate/significant/major]

### Changes Made

**Phase [N] — [artifact name]:**
- [Specific change 1]
- [Specific change 2]

**Phase [N+1] — [artifact name]:**
- [Cascaded change 1]

**Phase [N+2] — [artifact name]:**
- [Cascaded change 1]

### Files Modified
- `path/to/file1` — [what changed]
- `path/to/file2` — [what changed]

### Verification
- [ ] All affected artifacts are consistent
- [ ] All tests pass
- [ ] No orphaned references
- [ ] State.json updated

### Lessons Learned
[Optional: What could we have caught earlier? Should a discovery question be added?]
```

## Quality Checklist for Rework

Before marking rework as complete:

### Process
- [ ] Rework trigger correctly classified
- [ ] Origin phase correctly identified (not just symptom phase)
- [ ] Scope assessed and confirmed with user (for significant/major)
- [ ] Rework tracked in state.json with unique ID

### Artifacts
- [ ] Origin artifact updated with revision history
- [ ] All downstream artifacts cascaded
- [ ] No orphaned references to old design/contracts/specs
- [ ] Consistency check passed across all phases

### Implementation
- [ ] Code updated to match revised specs
- [ ] Tests updated to cover revised behavior
- [ ] All tests pass (including pre-existing tests)
- [ ] Linter and type checker pass

### Communication
- [ ] Other developers notified if integration contracts changed
- [ ] Blocked stories unblocked in state.json after rework completes
- [ ] Rework summary documented (for significant/major rework)

## Definition of Done

Rework is complete when:
- [ ] Origin phase artifact is updated
- [ ] All downstream artifacts are cascaded and consistent
- [ ] Implementation matches revised specs
- [ ] All tests pass
- [ ] State.json reflects completed rework
- [ ] User confirms: "Rework looks good, let's continue"