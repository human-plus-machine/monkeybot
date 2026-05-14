# memory-save
Save information to persistent memory so you can recall it later.

## When to use
Use this skill when the user asks you to remember something, or when you learn
something important that should persist across sessions.

## Steps
1. Decide on a descriptive filename (e.g. `user-preferences.md`, `project-context.md`)
2. Call write_file with:
   - path: `{memory_path}/{filename}.md`  (memory_path is in your context)
   - content: a well-structured markdown document with the information
3. Confirm to the user: "I've saved that to memory as `{filename}.md`"

## Example
User: "Remember that I prefer concise answers"
→ write_file(path="{memory_path}/user-preferences.md",
             content="# User Preferences\n\n- Prefers concise answers")
