# Subagents

Subagents let you decompose a complex agent into a set of specialised workers, each with its own focused system prompt and skills, without sacrificing any execution capabilities.

---

## Why Subagents

Every call to an LLM carries its full context window: conversation history, system prompt, skills manifest, memory, and pending tool outputs.  As an agent acquires more skills the context window grows, the model's effective attention narrows, and costs rise.

Subagents solve this by partitioning the context:

- The **orchestrator** sees only its own skills and a short description of each subagent.
- A **subagent** is spun up with its own isolated context window and skill manifest only when the orchestrator delegates a task to it via the `task` tool.
- Both agents share the **same backend** — the same filesystem, the same working directory, the same environment variables, and the same shell.  Subagents are a *context* boundary, not an execution boundary.

---

## How It Works

```
Orchestrator (main agent)
  Context: SOUL.md + orchestrator skills manifest
  Tools:   read_file, write_file, edit_file, ls, glob, grep, execute,
           write_todos, schedule_task, search_memory, task ←─ delegates here

  task("content-intel", "Research edtech trends for Q2 campaign")
        │
        ▼
  Subagent: content-intel
    Context: isolated — only content-intelligence skills manifest
    Tools:   read_file, write_file, edit_file, ls, glob, grep, execute,
             write_todos
    Reads SKILL.md → executes Python via `execute` → returns result to orchestrator
```

The orchestrator reads skill files and executes them with `execute` (shell).  Subagents do exactly the same thing — via the same backend — they just have a smaller, focused context window.

---

## Configuration

Add a `subagents:` section to `bot.yaml`.  If the section is absent, the bot behaves exactly as before (single flat agent, no `task` tool).

```yaml
# =============================================================================
# Subagents  (optional — omit for single-agent mode)
# =============================================================================
subagents:
  - name: content-intel
    description: >
      Research top-performing content in any domain, extract engagement
      patterns, and produce structured intelligence reports.
    skills:
      - ./skills/content-intelligence/

  - name: content-creation
    description: >
      Generate text posts, image prompts, and short-video scripts for
      social platforms given a brief and target persona.
    skills:
      - ./skills/content-creation/

  - name: icp-funnel
    description: >
      Research ICPs for any vertical, maintain persona tiers, map funnel
      stages, and track penetration metrics.
    skills:
      - ./skills/icp-funnel/

  - name: analytics
    description: >
      Query GA4, generate UTM links, collect baselines, and detect
      performance regressions.
    skills:
      - ./skills/analytics/
    prompt_file: ./prompts/analytics.md   # optional custom system prompt
    model: gemini-2.0-flash               # optional model override
```

### Field reference

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique identifier.  The orchestrator calls `task(name, ...)`. |
| `description` | yes | One-to-two sentence description.  The orchestrator uses this to decide which subagent to call. |
| `skills` | no | List of skill directory paths.  `SkillsMiddleware` loads each directory and injects the manifest into the subagent's system prompt. |
| `prompt_file` | no | Path to a Markdown file used as the subagent's system prompt.  Defaults to `"You are the <name> specialist."` |
| `model` | no | Model override in `provider:model-name` format (e.g. `gemini-2.0-flash`).  Defaults to the orchestrator's model. |

---

## Skills Directory Layout

Partition your `skills/` directory by subagent responsibility:

```
skills/                              # orchestrator skills
├── phase-transition/
│   ├── SKILL.md
│   └── phase_transition.py
└── gate-check/
    ├── SKILL.md
    └── gate_check.py

skills/content-intelligence/         # content-intel subagent skills
├── research-domain-content/
│   ├── SKILL.md
│   └── research_domain_content.py
├── score-content-engagement/
│   ├── SKILL.md
│   └── score_content_engagement.py
└── extract-content-patterns/
    ├── SKILL.md
    └── extract_content_patterns.py

skills/analytics/                    # analytics subagent skills
├── query-ga4/
│   ├── SKILL.md
│   └── query_ga4.py
└── generate-utm/
    ├── SKILL.md
    └── generate_utm.py
```

Point each subagent's `skills:` list at its directory.  A subagent can load from multiple directories if needed:

```yaml
- name: writer
  description: Generate content.
  skills:
    - ./skills/base/           # shared utilities
    - ./skills/content-creation/
```

---

## Writing Subagent System Prompts

When `prompt_file` is not set, the framework uses `"You are the <name> specialist."` — often enough for focused subagents.  For more control, create a Markdown file:

```markdown
<!-- prompts/analytics.md -->
You are the Analytics Specialist for Auriga OS.

Your responsibilities:
- Query GA4 for traffic, conversion, and engagement data
- Generate UTM-tagged links for all campaigns
- Detect regressions against 4-week baselines
- Report findings in the standard metrics format

You have access to the skills listed in your manifest.  Read each SKILL.md before
calling the corresponding script.  Write all output to ./data/analytics/.
```

Reference it in `bot.yaml`:

```yaml
- name: analytics
  description: Query GA4, generate UTMs, detect regressions.
  skills:
    - ./skills/analytics/
  prompt_file: ./prompts/analytics.md
```

---

## End-to-End Example

### 1. Create a subagent skill

```
skills/content-intelligence/research-domain-content/
├── SKILL.md
└── research_domain_content.py
```

`SKILL.md`:

```markdown
---
name: research-domain-content
description: >
  Research top-performing content for a given domain and audience.
  Returns a structured JSON report with titles, formats, and engagement scores.
---

# Research Domain Content

## When to Use
- User asks to research content trends for a vertical
- Kick-off phase of a new campaign requires competitive analysis

## How to Use
```python
python skills/content-intelligence/research-domain-content/research_domain_content.py \
  --domain "edtech" \
  --audience "higher-ed admins" \
  --output ./data/intelligence/edtech-report.json
```
```

`research_domain_content.py`:

```python
import argparse, json, pathlib

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    # ... research logic using Perplexity / Firecrawl / etc. ...
    report = {"domain": args.domain, "audience": args.audience, "insights": []}

    pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"Report written to {args.output}")

if __name__ == "__main__":
    main()
```

### 2. Configure in `bot.yaml`

```yaml
subagents:
  - name: content-intel
    description: Research top-performing content in any domain.
    skills:
      - ./skills/content-intelligence/
```

### 3. Observe orchestrator delegation

```
User: "Research edtech content trends for our Q2 campaign."

Orchestrator:
  → Decides content research is content-intel's job
  → Calls task("content-intel", "Research edtech content trends for Q2 campaign")

  content-intel subagent:
    → Reads ./skills/content-intelligence/research-domain-content/SKILL.md
    → execute("python skills/content-intelligence/research-domain-content/research_domain_content.py --domain edtech ...")
    → Returns structured report to orchestrator

Orchestrator:
  → Receives report, continues campaign planning
```

---

## Testing

Test subagent configuration parsing:

```python
import src.core.config as cfg_mod
from src.core.config import get_subagent_configs

def test_subagent_config_roundtrip(tmp_path, monkeypatch):
    (tmp_path / "bot.yaml").write_text("""
subagents:
  - name: content-intel
    description: Research content.
    skills:
      - ./skills/content-intelligence/
""")
    monkeypatch.chdir(tmp_path)
    cfg_mod._config_loaded = False
    cfg_mod._raw_yaml = None

    from src.core.config import load_bot_config
    load_bot_config()
    configs = get_subagent_configs()

    assert len(configs) == 1
    assert configs[0].name == "content-intel"
    assert configs[0].skills == ["./skills/content-intelligence/"]

    cfg_mod._config_loaded = False
    cfg_mod._raw_yaml = None
```

Test subagent skill execution independently:

```bash
python skills/content-intelligence/research-domain-content/research_domain_content.py \
  --domain "edtech" \
  --audience "higher-ed admins" \
  --output /tmp/test-report.json

cat /tmp/test-report.json
```

---

## Backward Compatibility

Omitting the `subagents:` section from `bot.yaml` preserves today's behaviour exactly:

- No `task` tool is exposed to the orchestrator.
- All skills are loaded into the single orchestrator context.
- `LocalShellBackend` is still used, giving the orchestrator native `read_file` and `execute` tools.

To migrate an existing single-agent deployment to the subagent model:

1. Create subdirectories under `skills/` for each domain.
2. Move existing skills into the appropriate subdirectory.
3. Add the `subagents:` section to `bot.yaml`.
4. Test each subagent skill in isolation before enabling.
