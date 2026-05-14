#!/usr/bin/env python3
"""Memory operations skill for Monkeybot."""
import argparse
import json
import sys
from pathlib import Path

MEMORY_DIR = Path("./data/memory/KNOWLEDGE_BASE")


def remember_fact(key: str, value: str) -> str:
    """Store a fact in memory."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    facts_file = MEMORY_DIR / "facts.json"
    
    # Load existing facts
    if facts_file.exists():
        with open(facts_file) as f:
            facts = json.load(f)
    else:
        facts = {"version": "1.0", "facts": {}}
    
    # Add/update fact
    from datetime import datetime
    now = datetime.now().isoformat()
    
    if key in facts["facts"]:
        facts["facts"][key]["value"] = value
        facts["facts"][key]["updated_at"] = now
    else:
        facts["facts"][key] = {
            "value": value,
            "created_at": now,
            "updated_at": now
        }
    
    # Write back
    with open(facts_file, 'w') as f:
        json.dump(facts, f, indent=2)
    
    return f"Remembered: {key} = {value}"


def recall_fact(key: str) -> str:
    """Retrieve a fact from memory."""
    facts_file = MEMORY_DIR / "facts.json"
    
    if not facts_file.exists():
        return f"No facts stored yet"
    
    with open(facts_file) as f:
        facts = json.load(f)
    
    fact = facts.get("facts", {}).get(key)
    if fact:
        return f"{key} = {fact['value']}"
    else:
        return f"Fact '{key}' not found"


def main():
    parser = argparse.ArgumentParser(description="Memory operations")
    parser.add_argument("--action", choices=["remember", "recall"], required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--value", default="")
    
    args = parser.parse_args()
    
    try:
        if args.action == "remember":
            if not args.value:
                raise ValueError("--value required for remember action")
            result = remember_fact(args.key, args.value)
        elif args.action == "recall":
            result = recall_fact(args.key)
        
        print(result)
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
