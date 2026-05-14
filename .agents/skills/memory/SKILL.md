---
name: memory
description: "Store and retrieve facts from memory"
metadata:
  monkeybot:
    requires:
      bins: ["python3"]
      files: ["./data/memory/"]
---

# Memory Skill

Store and retrieve user preferences and facts.

## Remember Fact

```bash
python3 skills/memory/memory.py --action remember --key preferred_language --value Python
```

## Recall Fact

```bash
python3 skills/memory/memory.py --action recall --key preferred_language
```
