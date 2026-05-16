---
name: acceptance-checklist
description: Phase 2B - Acceptance Checklist. Drafts a concrete, executable list of manual and automatable checks that confirm the feature is fully working from the outside. Produced after Phase 2 (User Stories) and executed in Phase 7.
---

# Phase 2B: Acceptance Checklist

## Purpose

Produce a concrete, executable checklist that answers: **"How do we know the feature actually works?"**

This is distinct from automated tests (Phase 5) and integration tests (Phase 6). Those confirm the code matches the spec. The acceptance checklist confirms the feature works from a user or operator perspective — real HTTP calls, real UI interactions, real edge cases a human would actually encounter.

The checklist is drafted here, after Phase 2, when the full story scope is known. It is executed in Phase 7, after integration is complete.

## When This Phase Runs

Phase 2B runs immediately after Phase 2 (User Stories) is approved and before Phase 3 (Code Spec) begins.

## Process

### Step A1: Draft from Design and Stories

Read the following artifacts before writing anything:
1. `design/1a-discovery.md` — Core flows and data model
2. `design/1b-contracts.md` — API contracts and payloads
3. `stories/user_stories.md` — All acceptance criteria from every story

From these, derive three categories of checks:

**Category 1 — Happy Path Flows**
The primary end-to-end flows a user would perform. Cover every story's main success case.

**Category 2 — Error and Edge Cases**
Things that should fail gracefully. Invalid inputs, missing auth, duplicate actions, empty states, boundary values.

**Category 3 — Integration Scenarios**
Flows that cross story boundaries — e.g. create data in Story 1, consume it in Story 2.

### Step A2: Classify Each Check

For each check, classify it:

| Type | Definition |
|------|------------|
| `agent-automatable` | The agent can run this via curl, CLI, or shell command in Phase 7 |
| `human-ui` | Requires a browser — the human must perform this step |
| `human-verify` | The agent can trigger it but the human must visually confirm the result |

**Aim for at least 50% `agent-automatable` checks** — anything with an API or CLI surface can be automated.

### Step A3: Write the Checklist

Save as `.monkeymode/{feature-name}/stories/2b-acceptance.md` using this format:

```markdown
# Acceptance Checklist: {feature-name}

**Feature:** {feature name}
**Written:** {date}
**Stories covered:** {list of story titles}

---

## Happy Path Flows

### AC-001: {Check Title}
**Type:** agent-automatable | human-ui | human-verify
**Story:** {story title this maps to}
**Acceptance Criterion:** {the exact criterion from stories/user_stories.md this satisfies}

**Steps:**
1. {concrete action — for agent: exact curl/CLI command with placeholders; for human: exact UI action}
2. {next step}

**Expected Result:**
{exact response body, status code, UI state, or observable outcome}

**Failure Indicators:**
{what a wrong result looks like — e.g. 500 instead of 201, empty list, missing field}

---

## Error and Edge Cases

### AC-00N: {Check Title}
...

---

## Integration Scenarios

### AC-00N: {Check Title}
...
```

### Step A4: Ask the User to Review

Present the checklist to the user and ask:

```
"Here's the acceptance checklist I've drafted for {feature-name}. It covers:
- {N} happy path flows ({N} agent-automatable, {N} human-ui)
- {N} error/edge cases ({N} agent-automatable, {N} human-verify)
- {N} integration scenarios ({N} agent-automatable, {N} human-ui)

Are there any user flows, edge cases, or integration scenarios I missed?
Any checks you'd like to add, remove, or reword before we proceed to Phase 3?"
```

Incorporate all feedback, update the file, then ask for final approval before advancing.

### Step A5: Update State

After the checklist is approved:

```json
{
  "current_phase": "3",
  "phase_status": {
    "acceptance_checklist": "completed"
  },
  "artifacts": {
      "acceptance_checklist": ".monkeymode/{feature-name}/stories/2b-acceptance.md"
  }
}
```

---

## Checklist Item Format Reference

### For `agent-automatable` items (API / backend)

```markdown
### AC-003: Add a product to favorites
**Type:** agent-automatable
**Story:** Favorites API
**Acceptance Criterion:** User can add a product to favorites

**Steps:**
1. Obtain auth token:
   ```bash
   TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email": "test@example.com", "password": "testpass"}' \
     | jq -r '.token')
   ```
2. Add a favorite:
   ```bash
   curl -s -X POST http://localhost:8000/favorites \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"product_id": "prod-001"}'
   ```

**Expected Result:**
```json
{
  "id": "<any UUID>",
  "product_id": "prod-001",
  "created_at": "<any ISO8601>"
}
```
Status: 201

**Failure Indicators:** Status 4xx/5xx, missing `id` field, wrong `product_id`
```

### For `agent-automatable` items (CLI)

```markdown
### AC-007: Run database migration
**Type:** agent-automatable
**Story:** Database Setup

**Steps:**
1. ```bash
   uv run alembic upgrade head
   ```

**Expected Result:** `INFO  [alembic.runtime.migration] Running upgrade ...` — no errors
**Failure Indicators:** Any ERROR or FAILED line in output
```

### For `human-ui` items

```markdown
### AC-012: Add item to favorites from product listing page
**Type:** human-ui
**Story:** Favorites UI Component
**Acceptance Criterion:** Heart icon on product card adds item to favorites

**Steps:**
1. Log in as test@example.com
2. Navigate to /products
3. Hover over any product card
4. Click the heart icon

**Expected Result:**
- Heart icon changes from outline to filled
- Toast notification appears: "Added to favorites"
- Icon remains filled after page refresh

**Failure Indicators:** Icon doesn't change, no toast, icon reverts after refresh
```

### For `human-verify` items

```markdown
### AC-015: Verify paginated favorites list
**Type:** human-verify
**Story:** Favorites API + Favorites UI

**Steps:**
1. Agent seeds 25 favorites:
   ```bash
   uv run python scripts/seed_favorites.py --user test@example.com --count 25
   ```
2. Human navigates to /favorites

**Expected Result:**
- First 20 favorites are shown
- "Load more" button is visible
- Clicking "Load more" loads the remaining 5

**Failure Indicators:** All 25 shown at once, no "Load more", clicking loads wrong items
```

---

## Quality Checklist

Before finalizing:

- [ ] Every user story's primary acceptance criterion has at least one check
- [ ] At least one error/edge case per story
- [ ] At least one cross-story integration scenario
- [ ] All `agent-automatable` items have exact commands with no ambiguous placeholders
- [ ] All `human-ui` items have step-by-step instructions a non-developer could follow
- [ ] Every check has clear expected result AND failure indicators
- [ ] User has reviewed and approved the checklist
