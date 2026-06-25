# Your agent

Replace this file with your system prompt (Markdown). The gateway reads it from `paths.agent_md` in `monkeybot_config/monkeybot.yaml`.

## Skills and shell

- Use `list_skills`, then `read_file` on each skill's `SKILL.md` before running commands it documents.
- `run_command` runs from **workspace root**; use **workspace-relative** paths in `argv` (e.g. `.agents/skills/my-skill/script.py`), not guessed absolute paths.

See `docs/getting-started.md` and `monkeybot_config/monkeybot.example.yaml` for tuning.
