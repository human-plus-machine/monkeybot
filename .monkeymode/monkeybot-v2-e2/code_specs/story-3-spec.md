# Code Spec: Story 3 — Built-in Skills

**Story:** User Story 3 — Built-in Skills  
**Design Reference:** 1A ADR-E2-006, 1B "Built-in Skills"  
**Date:** 2026-05-13  

## Implementation Summary

- **Files to Create:** 4 markdown files
- **Files to Modify:** 0 files
- **Estimated Complexity:** S

## Technical Context

**Key Gotchas:**
- `list_skills()` in `tools/skill_ops.py` reads the **first non-blank, non-heading line** after the H1 as the description. The description MUST be on the second line — do NOT insert a blank line between `# Title` and the description.
- No Python code in this story. All deliverables are markdown files.
- `{memory_path}` and `{agent_md_path}` inside skill content are literal placeholder strings — the agent reads them from its runtime context.

**Discovery contract (from E1):**
```python
list_skills(skills_path=".agents/skills")
# Scans {skills_path}/*/SKILL.md — first non-blank non-heading line = description
```

## Task Breakdown

### Task 1: Create all 4 SKILL.md files

**Dependencies:** None  
**Files to Create:**
- `.agents/skills/memory-save/SKILL.md`
- `.agents/skills/memory-search/SKILL.md`
- `.agents/skills/file-ops/SKILL.md`
- `.agents/skills/self-improve/SKILL.md`

**Content for each file:** Defined verbatim in user_stories.md Story 3 "Skill Content Specifications" section. Copy exactly — no modifications.

**Required structure for each SKILL.md:**
```
# <skill-name>
<description line — immediately after H1, no blank line>

## When to use
...

## Steps
1. ...
2. ...

## Example  (or ## Notes for file-ops)
...
```

**Verification:** Ensure description line (line 2 of each file) is non-blank and non-heading.

---

### Task 2: Test skill discovery

There is no separate test file to create for SKILL.md content — the acceptance criteria are validated through the existing `list_skills()` mechanism. Write a **single test** that exercises all 4 skills.

**Files:** The existing `tests/unit/test_tools.py` already exists. Check if skill discovery tests are there; if not, add a test. **Do NOT create a new test file** — append to `tests/unit/test_tools.py` if it doesn't already have a `list_skills` test, or create `tests/unit/test_skills.py` only if `test_tools.py` is unrelated.

**Test case:**
```python
def test_builtin_skills_discoverable(tmp_path):
    # Copy .agents/skills/ into tmp_path, call list_skills(), assert all 4 present
    # OR point list_skills at the real .agents/skills/ path
    result = list_skills(skills_path=".agents/skills")
    for name in ("memory-save", "memory-search", "file-ops", "self-improve"):
        assert name in result
```

Also verify:
- Each skill returns a non-empty description (second line extracted correctly)
- `filter="memory"` returns only `memory-save` and `memory-search`

## Final Verification

**Functionality:**
- [ ] All 4 SKILL.md files exist at correct paths
- [ ] `list_skills(".agents/skills")` returns all 4 skill names
- [ ] Each skill has a non-empty description extracted
- [ ] `filter="memory"` returns exactly 2 skills
- [ ] Each SKILL.md has: H1, description on line 2, `## Steps`, numbered steps, example/notes section

**Format:**
- [ ] No blank line between `# Title` and description line
- [ ] Markdown renders cleanly (no broken headings, correct list syntax)
