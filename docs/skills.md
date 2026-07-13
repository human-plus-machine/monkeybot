# Skills (monkeybot v2)

Skills are **directories** under `SKILLS_PATH` (scaffolded default `./skills`,
next to `workspace/`). They are trusted, committed inputs: the agent may read
them but cannot modify them at runtime. The gateway discovers them at
context-build time and lists **name** and **description** in the system prompt
so the model knows what is available.

---

## Layout

Each skill is a **folder** (one level under `SKILLS_PATH`) containing at least:

| File | Required | Role |
|---|---|---|
| `SKILL.md` | Yes | Human-facing instructions; the first substantive line after optional YAML frontmatter becomes the **short description** in the prompt. |

Additional files (e.g. `phases/*.md`, scripts) are optional; they are not part of harness discovery rules.

Example:

```
skills/
└── diagnostics/
    ├── SKILL.md
    └── check.md
```

---

## SKILL.md

Optional YAML frontmatter (between `---` lines) is skipped when picking the description line. Put a clear one-line summary immediately after the frontmatter (or at the top if you omit frontmatter) so it reads well in the compact **Skills** section of the system message.

Use the body of `SKILL.md` for detailed procedures, constraints, and examples for the model.

---

## Discovery rules

Discovery is implemented in `monkeybot.core.context._discover_skills`:

- Only **immediate subdirectories** of `SKILLS_PATH` are scanned.
- A directory is a skill if it contains **`SKILL.md`**.
- Skills are sorted by folder name for stable ordering.

---

## Tools and execution

File tools expose skills through the `skills/` virtual prefix. For example,
`read_file("skills/diagnostics/SKILL.md")` reads the skill above. The `skills/`
prefix always routes to `SKILLS_PATH`; every other relative tool path routes to
the writable workspace. There is no fallback between roots. Writes, edits, and
patches to `skills/...` are rejected, and symlinks escaping either root are
rejected.

The v2 loop exposes core tools (filesystem, memory search, MCP management, etc.) and merges **MCP** tool definitions from `MCP_CONFIG` when present. Following a skill is **model-driven** via `read_file` / `run_command` / MCP as described in `SKILL.md` and `AGENT.md`; there is no separate skill entry script wired by the harness.

Keep agent-written artifacts outside skills. In particular, the bundled browser
skill remains under `skills/browser/`, while browser playbooks and screenshots
go under `workspace/browser/`. This keeps browser data disposable on ephemeral
deployments and prevents it from being treated as trusted instructions.

For the complete agent-zone and container-enforcement contract, see
[Agent project layout](agent-layout.md).
