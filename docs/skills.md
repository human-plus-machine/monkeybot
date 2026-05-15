# Skills (MonkeyBot v2)

Skills are **directories** under `SKILLS_PATH` (default `./.agents/skills`). The gateway discovers them at context-build time and lists **name, description, and entry point** in the system prompt so the model knows what is available.

---

## Layout

Each skill is a **folder** (one level under `SKILLS_PATH`) containing:

| File | Required | Role |
|---|---|---|
| `SKILL.md` | Yes | Human-facing instructions; the first substantive line after optional YAML frontmatter becomes the **short description** in the prompt. |
| `run.py` **or** `main.py` | Yes (one of them) | Entry module; stored as e.g. `diagnostics/run.py` relative to `SKILLS_PATH`. |

Example:

```
.agents/skills/
└── diagnostics/
    ├── SKILL.md
    └── run.py
```

---

## SKILL.md

Optional YAML frontmatter (between `---` lines) is skipped when picking the description line. Put a clear one-line summary immediately after the frontmatter (or at the top if you omit frontmatter) so it reads well in the compact **Skills** section of the system message.

Use the body of `SKILL.md` for detailed procedures, constraints, and examples for the model.

---

## Discovery rules

Discovery is implemented in `monkeybot.core.context._discover_skills`:

- Only **immediate subdirectories** of `SKILLS_PATH` are scanned.
- A directory is a skill only if it contains **`SKILL.md`** and **`run.py` or `main.py`**. When both entry points are present, `run.py` wins.
- Skills are sorted by folder name for stable ordering.

---

## Tools and execution

The v2 loop exposes core tools (filesystem, memory search, MCP management, etc.) and merges **MCP** tool definitions from `MCP_CONFIG` when present. Invoking Python inside a skill is model-driven via those tools and your instructions in `SKILL.md` and `AGENT.md`; there is no separate legacy skill-loader manifest step.

For a concrete sample layout, see `examples/skills/` in this repository.
