# file-ops
Read and write files in the bot directory and the agent's storage directory.

## When to use
Use this skill when you need to read a configuration file, inspect a document,
or write output that should persist as a file.

## Allowed paths
- Bot directory (where AGENT.md lives) — read and write
- Memory directory — read and write
- Do NOT attempt to access paths outside these directories

## Steps — Reading a file
1. Call read_file(path="relative/path/from/bot/dir/file.txt")
2. Use the returned content in your response

## Steps — Writing a file
1. Decide on a clear filename and path
2. Call write_file(path="...", content="...")
3. Confirm the write to the user

## Notes
- Paths outside the allowed roots return "ERROR: Access denied"
- Use forward slashes even on Windows
