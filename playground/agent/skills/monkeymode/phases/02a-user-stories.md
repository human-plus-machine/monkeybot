---
name: user-stories-skill
description: Guides the decomposition of designs into parallelizable user stories with detailed technical context. 
---
 
# User Stories Skill
 
## Purpose
Transform a technical design into actionable user stories that developers can implement independently and in parallel.
 
## Discovery Questions (ALWAYS ASK FIRST!)
 
Before creating any stories, ask the following question:

1. **"Which components need to communicate?"**
   - Define API contracts upfront for parallel development
 
## Core Principles
 
### Decompose by Independent Components (NOT by Headcount)

Stories are created based on **architectural boundaries** — independent components that touch separate files. Up to 10 subagents implement stories in parallel.

```
✅ GOOD (truly independent):
Story 1: Embeddings Component (defines EmbeddingsInterface)
Story 2: Storage Component (defines StorageInterface)
Story 3: Config Component (defines ConfigInterface)
→ All use mocks for dependencies, integration happens in Phase 6
 
❌ BAD (has dependencies, NOT independent):
Story 1: Database schema
Story 2: API endpoints (depends on Story 1) ← BLOCKS parallel execution!
Story 3: UI components (depends on Story 2) ← BLOCKS parallel execution!
```
 
### Never Assume - Always Ask
- Don't assume priority - ask about business value
- If design is unclear, ask before creating stories
 
## Story Creation Process
 
### Step 1: Decompose by Independent Components
 
**Create one story per independent component:**
 
```
Each story must:
✅ Touch completely different files/modules
✅ Have ZERO dependencies on other stories
✅ Define clear interfaces/contracts for integration
✅ Use mocks for any external dependencies
✅ Be fully testable in isolation
 
❌ NEVER create stories with dependencies on each other
❌ NEVER create layered stories (DB → API → UI)
❌ NEVER make one developer wait for another
```
 
**Example: 3 developers on ACE Memory System**
 
```
Story 1: Embeddings Component
- Files: ace/embeddings/
- Defines: EmbeddingsInterface
- Implements: BedrockEmbeddings (real)
- Tests: With real Bedrock API
 
Story 2: Vector Store Component
- Files: ace/vector_store/
- Defines: VectorStoreInterface
- Implements: DatabricksVectorStore (real)
- Tests: With MockEmbeddings
 
Story 3: Storage Component
- Files: ace/storage/
- Defines: StorageInterface
- Implements: S3Storage (real)
- Tests: With mocked S3
 
Integration (Phase 6 / orchestrated):
- Wire real components together
- End-to-end tests
```
 
### Step 2: Define Integration Contracts
 
**CRITICAL: Define all interfaces upfront so developers can work in parallel**
 
**Note on Mocking Strategy:** The mock/interface pattern is recommended for maximum parallelization, but not mandatory. If the lead engineer prefers a different integration approach, that's acceptable. However, if components must be integrated sequentially after stories complete, clearly document this in the story and inform the lead engineer of the integration dependencies.
 
```python
# Story 1 defines this interface
class EmbeddingsInterface(ABC):
    """Contract for embedding generation."""
    
    @abstractmethod
    async def embed_query(self, text: str) -> List[float]:
        """Generate embedding vector.
        
        Args:
            text: Input text (max 8000 tokens)
            
        Returns:
            1024-dimensional float vector
            
        Raises:
            ValueError: If text empty or too long
            EmbeddingError: If generation fails
        """
        pass
 
# Story 2 uses mock for testing
class MockEmbeddings(EmbeddingsInterface):
    async def embed_query(self, text: str) -> List[float]:
        return [0.1] * 1024  # Mock enables parallel development
```
 
### Step 3: Write Complete Story Details
 
Use this template for each story:
 
```markdown
## Story [N]: [Action] [Component]
 
**Repository:** [repo-name]
**Type:** Feature
**Priority:** High
**Size:** M (3-5 days)
**Dependencies:** NONE (fully parallel)
 
### Description
As a [user type],
I want [capability],
So that [business value].
 
### Technical Context
- **Affected modules:** [module] (new)
- **Design reference:** design.md "[Section]" section
- **Key files to create:**
  - `src/[module]/__init__.py`
  - `src/[module]/[component].py` (main implementation)
  - `src/[module]/interfaces.py` (contracts for other stories)
  - `src/[module]/mocks.py` (mocks for testing)
  - `tests/[module]/test_[component].py`
- **Patterns to follow:** [similar component pattern]
- **Dependencies:** NONE (no dependencies on other stories)
  - Note: Stories CAN depend on existing codebase, libraries, and infrastructure
 
### Integration Contracts
 
**Interfaces Defined by This Story:**
```python
# src/[module]/interfaces.py
class [Component]Interface(ABC):
    @abstractmethod
    async def method_name(self, param: Type) -> ReturnType:
        """Contract description with full type info."""
        pass
```
 
**Interfaces Used by This Story:**
```python
# Uses mocks for parallel development
from other_module.interfaces import OtherInterface
from other_module.mocks import MockOther
 
# Tests use MockOther() instead of real implementation
```
 
**Integration Timeline:**
- Phase 4: Fully functional with mocks, all tests pass
- Phase 6: Integration wires real implementations
 
### Acceptance Criteria
- [ ] **Given** [valid input], **When** [method called], **Then** [expected output]
- [ ] **Given** [invalid input], **When** [method called], **Then** [appropriate error]
- [ ] **Given** [edge case], **When** [method called], **Then** [handled correctly]
- [ ] Interface defined in interfaces.py with complete docstrings
- [ ] Mock implementation provided in mocks.py
- [ ] Unit tests cover: happy path, errors, edge cases (using mocks)
- [ ] All tests pass independently (no external dependencies)
 
### Implementation Details
[Paste relevant sections from design.md - function signatures, data models, error handling]
 
### Out of Scope
- Integration with other components (Phase 6 orchestrated)
- [Other exclusions to prevent scope creep]
 
### Notes for Developer
- Other stories will use your interface - make it clear!
- Provide good mocks - other devs depend on them
- [Helpful context, gotchas, recommendations]
```
 
### Step 4: Create Parallelization Plan
 
```markdown
## Parallelization Plan
 
**Core Components (ZERO dependencies)**
- Story 1: [Component A]
- Story 2: [Component B]
- Story 3: [Component C]

All stories start Day 1. No waiting.

**Integration (Phase 6 / orchestrated)**
- Wire components together
- End-to-end tests

**Future: Advanced Features** (if needed)
- Story X: [Feature A]
- Story Y: [Feature B]
```
 
**Key Rule:** If you see arrows (→) between stories, YOU'RE DOING IT WRONG!
 
### Step 5: Validate Quality
 
**True Parallelization Check:**
```
✅ One story per independent component
✅ ZERO dependencies between stories (can depend on existing codebase)
✅ Each story touches completely different files
✅ All integration contracts defined upfront
✅ Each story testable with mocks
✅ Integration handled in Phase 6
 
Test: Can all stories run in parallel without file conflicts?
If NO → redesign stories!
```
 
**Completeness Check:**
```
✅ Exact files to create/modify listed
✅ Patterns to follow referenced
✅ Design decisions linked
✅ Acceptance criteria testable
✅ Dependencies explicit (NONE for all stories)
✅ Interfaces have complete type signatures
✅ Mock implementations provided
```
 
## Story Template Variations
 
### For API/Backend Components
```markdown
**Key files to create:**
- `src/[module]/[component].py` (implementation)
- `src/[module]/interfaces.py` (contracts)
- `src/[module]/mocks.py` (mocks)
- `tests/[module]/test_[component].py`
```
 
### For UI Components
```markdown
**Key files to create:**
- `src/app/[feature]/components/[component]/[component].component.ts`
- `src/app/[feature]/components/[component]/[component].component.html`
- `src/app/[feature]/components/[component]/[component].component.scss`
- `src/app/[feature]/components/[component]/[component].component.spec.ts`
- `src/app/[feature]/services/[feature].service.ts`
- `src/app/[feature]/services/[feature].service.spec.ts`
```
 
### For Database/Infrastructure
```markdown
**Key files to create:**
- `scripts/setup_[resource].py` (idempotent setup)
- `scripts/[resource]_schema.sql` (if applicable)
- `tests/test_[resource]_setup.py`
 
**Note:** Make setup scripts idempotent and testable
```
 
## Output Quality Checklist
 
Before finalizing, verify:
 
### Discovery
- [ ] Asked: "What's your timeline?"
- [ ] Asked: "Which components communicate?"
- [ ] Created one story per independent component
 
### Parallelization
- [ ] Stories have ZERO dependencies
- [ ] Each story touches different files/modules
- [ ] All contracts defined upfront
- [ ] Mocks provided for testing
- [ ] Test: All N devs can start Day 1
 
### Story Quality
- [ ] All required sections present
- [ ] Given/When/Then acceptance criteria
- [ ] Exact files listed
- [ ] Patterns referenced
- [ ] Out of scope defined
- [ ] Interfaces with complete types
- [ ] Integration timeline specified
 
## Anti-Patterns to Avoid
 
❌ **Stories with dependencies**
```
Story 1: Database Schema
Story 2: API (depends on Story 1) ← BLOCKS Dev 2!
```
 
✅ **Independent components**
```
Story 1: Embeddings (defines interface)
Story 2: VectorStore (uses MockEmbeddings)
→ Both start Day 1!
```
 
---
 
❌ **Vague acceptance criteria**
```
- [ ] API works correctly
```
 
✅ **Specific acceptance criteria**
```
- [ ] **Given** valid ID, **When** POST /api, **Then** 201 with object
- [ ] **Given** invalid ID, **When** POST /api, **Then** 404 with error
```
 
---
 
❌ **Missing technical context**
```
"Implement favorites API"
```
 
✅ **Complete technical context**
```
"Implement favorites API"
- Files: src/favorites/controller.ts (create)
- Pattern: Follow src/users/controller.ts
- Design: See design.md "API Contracts"
```
 
---
 
❌ **Vague contracts**
```python
def save(self, data): pass
```
 
✅ **Clear contracts**
```python
@abstractmethod
async def save(self, data: Dict[str, Any]) -> bool:
    """Save data to store.
    
    Args:
        data: Dict with required keys: id, content
        
    Returns:
        True if saved, False if failed
        
    Raises:
        ValueError: If data missing required keys
    """
    pass
```
 
## Update State File After Stories Are Created

**CRITICAL:** After generating all user stories, you MUST update the `state.json` file to add entries for each story in the `stories` object.

This enables parallel development without merge conflicts - each developer only modifies their own story's entry.

```json
{
  "stories": {
    "story-1-embeddings-component": {
      "title": "Embeddings Component",
      "status": "not_started",
      "code_spec_path": null,
      "assigned_to": null,
      "current_task": null,
      "files_to_create": [],
      "files_to_modify": [],
      "blocked_by_rework": null,
      "verification": {
        "result": "pending",
        "rework_attempts": 0,
        "rework_summary": null,
        "escalated_issues": [],
        "last_checked_at": null
      },
      "last_updated": "2024-01-15T10:30:00Z"
    },
    "story-2-vector-store": {
      "title": "Vector Store Component",
      "status": "not_started",
      "code_spec_path": null,
      "assigned_to": null,
      "current_task": null,
      "files_to_create": [],
      "files_to_modify": [],
      "blocked_by_rework": null,
      "verification": {
        "result": "pending",
        "rework_attempts": 0,
        "rework_summary": null,
        "escalated_issues": [],
        "last_checked_at": null
      },
      "last_updated": "2024-01-15T10:30:00Z"
    },
    "story-3-storage-component": {
      "title": "Storage Component",
      "status": "not_started",
      "code_spec_path": null,
      "assigned_to": null,
      "current_task": null,
      "files_to_create": [],
      "files_to_modify": [],
      "blocked_by_rework": null,
      "verification": {
        "result": "pending",
        "rework_attempts": 0,
        "rework_summary": null,
        "escalated_issues": [],
        "last_checked_at": null
      },
      "last_updated": "2024-01-15T10:30:00Z"
    }
  }
}
```

**Note:** `files_to_create` and `files_to_modify` are populated in Phase 3 (Code Spec). `verification` fields are populated in Phase 5. All fields are initialized here with defaults so the schema is complete from the start.

**Story key format:** `story-{N}-{kebab-case-title}` (e.g., `story-1-embeddings-component`)

**Status values:**
- `not_started` - Story created, no work begun
- `code_spec` - Code spec is being written (Phase 3)
- `implementation` - Implementation in progress (Phase 4)
- `implementation_complete` - Implementation finished, ready for verification (Phase 5)
- `verified` - Passed verification (Phase 5)
- `verification_failed` - Failed verification, needs rework (Phase 5)
- `integrated` - Wired into the full feature (Phase 6)
- `completed` - Story fully implemented, verified, and integrated
- `failed` - Story failed with unrecoverable errors

## Final Reminder

**The goal:** Independent stories that can be implemented in parallel (by subagents or developers), with clear contracts enabling seamless integration.

**Success criteria:**
- No story blocks another
- No merge conflicts (each dev only modifies their story's state entry)
- All stories fully testable with mocks
- Integration is straightforward (just wire real implementations)

## Handoff to Phase 2B

After the user approves the user stories, immediately proceed to Phase 2B (Acceptance Checklist) before starting Phase 3 (Code Spec).

Phase 2B uses the approved user stories and design docs to draft the acceptance checklist — the concrete list of curl commands, CLI checks, and UI steps that will be used in Phase 7 to confirm the feature is fully working.

**Do not skip Phase 2B.** Without it, Phase 7 has no checklist to execute.