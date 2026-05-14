#!/usr/bin/env python3
"""File operations skill for Monkeybot."""
import argparse
import os
import sys


def read_file(path: str) -> str:
    """Read file contents."""
    with open(path, 'r') as f:
        return f.read()


def list_directory(path: str) -> str:
    """List directory contents."""
    items = os.listdir(path)
    return '\n'.join(sorted(items))


def write_file(path: str, content: str) -> str:
    """Write content to file."""
    with open(path, 'w') as f:
        f.write(content)
    return f"Wrote {len(content)} bytes to {path}"


def main():
    parser = argparse.ArgumentParser(description="File operations")
    parser.add_argument("--action", choices=["read", "list", "write"], required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--content", default="")
    
    args = parser.parse_args()
    
    try:
        if args.action == "read":
            result = read_file(args.path)
        elif args.action == "list":
            result = list_directory(args.path)
        elif args.action == "write":
            result = write_file(args.path, args.content)
        
        print(result)
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
