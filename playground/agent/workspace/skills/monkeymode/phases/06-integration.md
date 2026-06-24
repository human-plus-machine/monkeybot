---
name: integration
description: Phase 6 - Integration. Wires independently-implemented stories together — shared files, router registrations, dependency injection, configuration, and end-to-end testing.
---

# Phase 6: Integration

## Purpose

Wire independently-implemented and verified stories together into a cohesive, working feature. During Phases 4-5, each story was implemented and verified in isolation. Phase 6 handles the cross-story concerns that couldn't be done in parallel:

- Shared file merges (`__init__.py`, router registrations, config)
- Dependency injection wiring
- Cross-story contract validation
- End-to-end integration testing
- Final feature-level verification

## When This Phase Runs

Phase 6 begins when:
- All stories (or a user-approved subset) have status `verified` from Phase 5
- The full test suite passes
- The user confirms readiness to proceed

## Orchestrator Workflow

### Step I1: Identify Integration Points

Read all verified stories' code specs and identify cross-story touchpoints:

```
For each pair of verified stories (A, B):
  1. Shared files — Files that both stories intended to modify
     (these were separated into sequential batches during Phase 4)
  2. Cross-story imports — Does Story A import from Story B's modules?
  3. Shared registrations — Router registrations, DI bindings, config entries
  4. Contract boundaries — Does Story A call Story B's interfaces?
```

Build an integration checklist:

```markdown
## Integration Checklist

### Shared File Merges
- [ ] `src/__init__.py` — Add exports from Story 1 and Story 2
- [ ] `src/app.py` — Register routers for Story 1, Story 2, Story 3
- [ ] `src/config.py` — Add config entries for Story 2 and Story 3

### Cross-Story Wiring
- [ ] Story 2 depends on Story 1's EmbeddingsInterface — verify import works
- [ ] Story 3 depends on Story 2's VectorStoreInterface — verify import works

### Dependency Injection
- [ ] Wire EmbeddingsService into DI container
- [ ] Wire VectorStoreService into DI container
- [ ] Wire StorageService into DI container

### Configuration
- [ ] Add environment variables for Story 2 (VECTOR_DB_URL)
- [ ] Add environment variables for Story 3 (S3_BUCKET_NAME)
```

Present this checklist to the user before proceeding.

### Step I2: Merge Shared Files

For each shared file, apply changes from all stories:

1. **Read each story's code spec** to understand what changes were intended for the shared file
2. **Apply changes in story order** (Story 1 first, then Story 2, etc.)
3. **Resolve any conflicts** — If two stories add to the same section, combine them logically
4. **Run tests after each merge** to catch issues early

**Common shared files:**

| File | Typical Changes |
|------|----------------|
| `__init__.py` | Add exports for new modules |
| `app.py` / `main.py` | Register new routers, middleware |
| `routes.py` / `urls.py` | Add new route handlers |
| `config.py` / `settings.py` | Add new config entries |
| DI container setup | Register new services, repositories |
| Migration files | Create/order migration chain |

### Step I3: Wire Cross-Story Dependencies

Verify that cross-story interfaces work correctly:

1. **Import verification** — Can Story B import Story A's exports?
2. **Type compatibility** — Do the types match across story boundaries?
3. **Contract compliance** — Does Story A's implementation satisfy the interface that Story B expects?

```python
# Example: Verify Story 2 can use Story 1's interface
from src.embeddings.interface import EmbeddingsInterface
from src.vector_store.service import VectorStoreService

# This should work without type errors:
embeddings: EmbeddingsInterface = get_embeddings_service()
vector_store = VectorStoreService(embeddings=embeddings)
```

### Step I4: Write Integration Tests

Write end-to-end tests that exercise the full feature flow across multiple stories:

```python
class TestFeatureIntegration:
    """End-to-end integration tests for the complete feature."""
    
    async def test_full_flow(self, client, auth_token):
        """Test the complete feature flow across all stories."""
        # Step 1: Use Story 1's functionality
        # Step 2: Use Story 2's functionality (depends on Story 1)
        # Step 3: Use Story 3's functionality (depends on Story 2)
        # Verify the end-to-end result
        pass
    
    async def test_error_propagation_across_stories(self, client, auth_token):
        """Test that errors in one story's component propagate correctly."""
        pass
```

Integration tests should cover:
- Happy path through the full feature
- Error propagation across story boundaries
- Edge cases at integration points (e.g., Story B handles Story A returning empty results)
- Performance of the combined flow (no N+1 issues at integration points)

### Step I5: Run Full Verification

After all integration work is complete:

1. **Run the full test suite** — All unit tests + new integration tests
2. **Run linter** across the entire project
3. **Run type checker** across the entire project
4. **Verify no regressions** — All previously passing tests still pass

### Step I6: Report to User

```
"Phase 6 — Integration complete:

Shared files merged:
  - src/__init__.py — exports from 3 stories
  - src/app.py — 3 routers registered
  - src/config.py — 2 new config entries

Cross-story wiring:
  - Story 2 -> Story 1 (EmbeddingsInterface): verified
  - Story 3 -> Story 2 (VectorStoreInterface): verified

Integration tests: 5 new tests, all passing
Full test suite: 45/45 passing
Linter: clean

All stories implemented, verified, and integrated.
Ready to proceed to Phase 7 (Acceptance) — running the acceptance checklist to confirm the feature works end-to-end?"
```

### Step I7: Update State

Mark integration as complete and advance to Phase 7:

```json
{
  "current_phase": "7",
  "stories": {
    "story-1-embeddings": { "status": "integrated" },
    "story-2-vector-store": { "status": "integrated" },
    "story-3-storage": { "status": "integrated" }
  },
  "integration": {
    "status": "completed",
    "shared_files_merged": ["src/__init__.py", "src/app.py", "src/config.py"],
    "integration_tests_added": 5,
    "completed_at": "ISO8601 timestamp"
  }
}
```

## Handling Integration Failures

### Import/Type Errors

If cross-story imports fail:
1. Check if the exporting story's `__init__.py` was properly merged
2. Check if the function signatures match the contract from Phase 1B
3. If signatures don't match, this is a contract mismatch — load `phases/rework.md`

### Test Failures at Integration Points

If integration tests fail:
1. Determine which story's component is at fault
2. If the component works correctly per its spec but the integration fails, the issue is in the integration contract (Phase 1B/2)
3. If the component is wrong per its spec, send it back through Phase 5 (Verification + Rework)

### Shared File Merge Conflicts

If two stories' changes to a shared file conflict:
1. Read both stories' code specs for their intended changes
2. Determine the correct combined state
3. Apply both changes logically (usually additive — both add exports, both add routes)
4. If the changes are contradictory, escalate to the user