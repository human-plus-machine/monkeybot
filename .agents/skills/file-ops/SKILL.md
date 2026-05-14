---
name: file-ops
description: "File operations (read, write, list)"
metadata:
  monkeybot:
    requires:
      bins: ["cat", "ls", "python3"]
      files: ["./data/memory/"]
---

# File Operations Skill

Read, write, and list files in allowed directories.

## Read File

```bash
python3 skills/file-ops/file_ops.py --action read --path ./data/memory/test.txt
```

## List Directory

```bash
python3 skills/file-ops/file_ops.py --action list --path ./data/memory/
```

## Write File

```bash
python3 skills/file-ops/file_ops.py --action write --path ./data/memory/test.txt --content "Hello World"
```
