# self-improve
Update your own AGENT.md to capture lessons learned and improve future behaviour.

## When to use
Use this skill after completing a task where you learned something that would
make you more effective in future sessions — a new pattern, a user preference,
a useful fact about the project.

## Steps
1. Read your AGENT.md with read_file(path="{agent_md_path}")
2. Identify the most relevant section to append to (or create a new ## section)
3. Draft a concise lesson — 1–3 bullet points maximum
4. Write the updated AGENT.md back with write_file
5. Tell the user: "I've updated my instructions to remember that."

## Rules
- Only add information, never remove existing instructions
- Keep additions concise — avoid padding
- Use the same markdown style as the existing AGENT.md
