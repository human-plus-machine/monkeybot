# memory-search
Search your persistent memory to recall previously saved information.

## When to use
Use this skill when the user asks about something you might have saved before,
or when context from a previous session would be helpful.

## Steps
1. Call search_memory with a descriptive query string
2. Review the returned results
3. If results are relevant, use them to inform your response
4. If no results found, tell the user you don't have that in memory

## Example
User: "What are my preferences?"
→ search_memory(query="user preferences", top_k=5)
→ Summarise relevant results in your response
