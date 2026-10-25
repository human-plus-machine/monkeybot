# 6.M Skills and slash skills (`SKILL`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`.

Existing: T1 `test_context.py`, `test_workspace_symlinks.py`, `test_slash_skills.py`. T4 `skills/skill_invocation` (asserts `list_skills` only).

| ID | Case | Asserts |
|---|---|---|
| T1-SKILL-01 | `discover_rules` | Only immediate subdirs with `SKILL.md`; nested ignored; frontmatter `description` wins over first line. |
| T2-SKILL-01 | `list_then_read_skill` | `list_skills` then `read_file skills/diagnostics/SKILL.md` returns the file. |
| T2-SKILL-02 | `skill_write_denied` | Write/replace/patch under `skills/`: `skills_read_only`; bytes unchanged. |
| T2-SKILL-03 | `skill_symlink_escape` | Escaping symlink under skills: `path_escape`. |
| T2-SKILL-04 | `skills_in_prompt` | System prompt `## Skills` lists names + descriptions. |
| T2-SKILL-05 | `skills_path_hot_reload` | Changing `paths.skills_path` applies on the next turn (volatile `skills` source). |
| T2-SKILL-06 | `slash_unknown_skill` | `/nope`: treated as text, no invoked section. |
| T4-SKILL-01 | `skill_invocation` (migrate, extended) | `required_tools: [list_skills, read_file]`. |
| T5-SKILL-01 | trials of T4-SKILL-01 | Reliability. |
