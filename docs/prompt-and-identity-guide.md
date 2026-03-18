# Prompt & Identity Guide

monkey-bot uses several files and settings to shape what your agent knows, how it behaves, and what it checks. This guide explains each one, the difference between them, and exactly when the agent reads or uses each.

---

## The Mental Model

Think of these as different layers of context your agent carries:

```
┌──────────────────────────────────────────────────────────────┐
│  What the agent IS              →  SOUL.md + IDENTITY.md     │
│  What the agent DOES            →  user_system_prompt        │
│  Who the agent talks to         →  USER.md                   │
│  What the agent remembers       →  INDEX.md                  │
│  What the agent checks on       →  HEARTBEAT.md              │
└──────────────────────────────────────────────────────────────┘
```

A person analogy:
- `SOUL.md` = their personality
- `IDENTITY.md` = their job description
- `user_system_prompt` = the instructions their manager gave them
- `USER.md` = what they know about you personally
- `INDEX.md` = their filing cabinet index
- `HEARTBEAT.md` = the checklist they run at the end of each shift

---

## When Is Each File Read?

| File / Setting | When Read | By What |
|---|---|---|
| `SOUL.md` | Every agent startup | `build_deep_agent()` — injected into Layer 1 of system prompt |
| `IDENTITY.md` | Every agent startup | `build_deep_agent()` — injected into Layer 1 of system prompt |
| `USER.md` | Every agent startup | `build_deep_agent()` — injected into Layer 1 of system prompt |
| `INDEX.md` | Every agent startup | `build_deep_agent()` — injected into Layer 1 of system prompt |
| `user_system_prompt` | Every agent startup | `build_deep_agent()` — becomes Layer 3 of system prompt |
| `HEARTBEAT.md` | During each heartbeat job | `HeartbeatHandler` — not at startup |

**Key insight:** `SOUL`, `IDENTITY`, `USER`, and `INDEX` are all loaded at startup and permanently part of the system prompt for every conversation. `HEARTBEAT.md` is only ever read during a scheduled heartbeat cycle.

---

## SOUL.md — Who the Agent Is

**Purpose:** Personality, tone, communication values.

**When used:** Baked into every conversation via the system prompt. The agent uses it unconsciously — it shapes *how* it responds, not *what* it does.

**Where it lives:** `{memory_dir}/SOUL.md` (default: `./data/memory/SOUL.md`)

**Token budget:** Under 500 tokens. Keep it tight.

**What to put in it:**

```markdown
I am direct and precise. I don't pad responses with pleasantries.
I use bullet points for lists, not run-on paragraphs.
I prefer concrete examples over abstract explanations.
When I'm uncertain, I say "I'm not sure" rather than guessing.
I match my technical depth to who I'm talking to.
I never apologize excessively — I just help.
```

**What NOT to put in it:**
- Operational instructions (that's `IDENTITY.md`)
- Task-specific rules (that's `user_system_prompt`)
- Long lists of capabilities

---

## IDENTITY.md — What the Agent's Role Is

**Purpose:** Operational context. What this agent does, what tools it has, who it serves, where it lives.

**When used:** Baked into every conversation. The agent uses it to understand its own capabilities and context before responding.

**Where it lives:** `{memory_dir}/IDENTITY.md`

**Token budget:** Under 800 tokens.

**What to put in it:**

```markdown
I am the engineering assistant for the Platform team at Acme Corp.

My primary users: Alice Chen (alice@acme.com) and Bob Davis (bob@acme.com).

I have access to:
- The file-ops skill for reading/writing files in my workspace
- The run_diagnostics skill for system health checks
- The schedule_task tool for scheduling background jobs
- The search_memory tool for searching past conversations

My memory is stored at gs://acme-bot-memory/data/memory/.
My workspace root is /app.

I post scheduled job results to the #platform-alerts Google Chat space.
```

**What NOT to put in it:**
- Personality (that's `SOUL.md`)
- Business rules for how to answer questions (that's `user_system_prompt`)

---

## user_system_prompt — What the Agent Should Do

**Purpose:** Your domain-specific instructions. This is the equivalent of a manager briefing the agent before it starts working.

**When used:** Baked into every conversation as Layer 3 of the system prompt — the outermost layer the agent reads before responding.

**Where it's set:** Passed directly to `build_deep_agent()` in `src/main.py`.

**What to put in it:**

```python
agent = build_deep_agent(
    model=model,
    tools=tools,
    user_system_prompt="""You are a customer support assistant for Acme Corp.

Your responsibilities:
- Answer questions about product pricing, features, and availability
- Help users troubleshoot issues using the diagnostics skill
- Escalate complex issues by scheduling a follow-up with the engineering team

Rules:
- Always acknowledge the user's question before answering
- Never reveal internal pricing tiers to non-enterprise users
- If you can't resolve an issue, schedule a follow-up within 24 hours

Tone: Helpful, professional, and concise. Max 3 bullet points per response.
""",
)
```

**What NOT to put in it:**
- Personality traits (that's `SOUL.md`)
- Operational context like tool names or file paths (that's `IDENTITY.md`)
- Thousands of tokens of documentation — keep it focused

**Relationship to SOUL and IDENTITY:**

The system prompt the agent actually receives looks like this (in order):

```
[Layer 1: Framework stuff — skills manifest, memory instructions, tool usage]
  └── Includes: IDENTITY.md content
  └── Includes: SOUL.md content
  └── Includes: USER.md content
  └── Includes: INDEX.md content

[Layer 2: Generic agent capabilities description]

[Layer 3: YOUR user_system_prompt]
```

Your `user_system_prompt` is always the last thing the agent reads before responding. It has the most direct influence on behavior.

---

## USER.md — What the Agent Knows About the User

**Purpose:** Persistent preferences, context, and facts about the primary user(s). Updated over time as the agent learns more.

**When used:** Baked into every conversation — the agent personalizes responses using this file.

**Where it lives:** `{memory_dir}/USER.md`

**What to put in it:**

```markdown
## Alice (alice@acme.com)
- Timezone: America/New_York
- Prefers concise responses — 3 bullet points max
- Strong Python background, needs no basic explanations
- Usually messages between 9am–6pm ET
- Wants urgent alerts even outside business hours

## Bob (bob@acme.com)
- Timezone: America/Los_Angeles
- Prefers detailed explanations with code examples
- Less familiar with Kubernetes — explain k8s concepts simply
- Messages mostly on Slack but sometimes here
```

**What NOT to put in it:**
- Sensitive PII beyond what's needed for personalization
- Information that changes frequently (that goes in raw observations)

**Who updates it:** You write the initial version. The agent can update it as it learns (if you instruct it to in your `user_system_prompt`).

---

## INDEX.md — The Agent's Memory Map

**Purpose:** A structured map of everything stored in the agent's memory directory. Lets the agent know what it can look up without reading every file.

**When used:** Read at startup. The agent references it when deciding whether to search memory for context before answering.

**Where it lives:** `{memory_dir}/INDEX.md`

**Token budget:** Under 1000 tokens. The framework warns if it exceeds this.

**What to put in it:**

```markdown
## episodic/
- 2026-03-01-api-outage.md: 2-hour API gateway outage, root cause DNS misconfiguration
- 2026-02-28-team-architecture-review.md: Team decided to adopt event-driven architecture in Q2

## semantic/
- kubernetes-runbook.md: Common k8s operations, pod restarts, scaling, debugging
- api-rate-limits.md: Rate limits for all downstream APIs (Stripe, Salesforce, SendGrid)
- deployment-checklist.md: Pre-deploy checklist for production releases

## procedural/
- on-call-escalation.md: Who to page and when during incidents
```

**Who maintains it:** The LLM Council updates it automatically after each heartbeat cycle (if `HEARTBEAT_COUNCIL_ENABLED=true`). You can also maintain it manually.

**What NOT to put in it:**
- The actual content of memory files — just summaries and paths
- Entries for files that don't exist yet

---

## HEARTBEAT.md — The Agent's Self-Check Instructions

**Purpose:** Instructions for what the agent should review and report on during a scheduled heartbeat check.

**When used:** ONLY during heartbeat jobs — not at startup, not during normal conversations. The `HeartbeatHandler` reads this file before invoking the agent for a self-check.

**Where it lives:** `{memory_dir}/HEARTBEAT.md`

**Required:** Only if `HEARTBEAT_ENABLED=true`.

**What to put in it:**

```markdown
During your workspace check, review the following:

1. Check data/memory/raw/ for any new unprocessed observation files
2. Look at INDEX.md for any items marked urgent or needing follow-up
3. Check the scheduler jobs file for any failed jobs (status: failed)
4. Review whether any tasks from the last standup are overdue

When you respond, always include:
- URGENT: yes/no  (is there anything requiring immediate attention?)
- SUMMARY: 1-2 sentences about the current workspace state

If URGENT is yes, describe what specifically needs attention.
```

**What NOT to put in it:**
- General personality or behavior rules — those are in SOUL.md
- Capabilities description — that's IDENTITY.md
- Content that should be in every conversation

---

## Quick Decision Guide

**"I want to change how the agent talks"** → Edit `SOUL.md`

**"I want to tell the agent what it is and what tools it has"** → Edit `IDENTITY.md`

**"I want to give the agent business rules and task instructions"** → Edit `user_system_prompt` in `src/main.py`

**"I want the agent to remember something about a specific user"** → Edit `USER.md`

**"I want to update what's indexed in the agent's memory"** → Edit `INDEX.md` (or let LLM Council do it)

**"I want to change what the agent reviews during its scheduled check-ins"** → Edit `HEARTBEAT.md`

---

## Full File Location Reference

All files live in your memory directory (default: `./data/memory/`):

```
data/memory/
├── SOUL.md          ← Personality (startup)
├── IDENTITY.md      ← Role + capabilities (startup)
├── USER.md          ← User preferences (startup)
├── INDEX.md         ← Memory map (startup)
├── HEARTBEAT.md     ← Self-check instructions (heartbeat only)
├── scheduler/
│   └── jobs.json    ← Scheduled jobs (auto-managed)
├── episodic/        ← Time-based event memories
├── semantic/        ← Facts and knowledge
├── procedural/      ← How-to guides
└── raw/             ← Unprocessed observations (Council input)
```

None of these files are required to start. The framework works without any of them — they progressively enrich your agent as you add them.
