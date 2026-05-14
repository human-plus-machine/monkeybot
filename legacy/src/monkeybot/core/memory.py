from __future__ import annotations

from pathlib import Path


def save_memory(memory_path: str, filename: str, content: str) -> str:
    """Write ``{memory_path}/{filename}.md``, creating parent directories if needed.

    Returns a human-readable confirmation string on success.
    """
    p = Path(memory_path) / f"{filename}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Success: saved memory/{filename}.md"


def search_memory(query: str, memory_path: str, max_results: int = 5) -> str:
    """Keyword search over ``*.md`` files under *memory_path*.

    Score = number of distinct query words found in a file (case-insensitive).
    Returns formatted excerpts (first 500 chars per file) sorted by score descending,
    limited to *max_results* files.
    """
    p = Path(memory_path)
    if not p.exists():
        return "No memory files found."

    keywords = [k.lower() for k in query.split() if k]
    results: list[tuple[int, Path, str]] = []

    for f in sorted(p.glob("**/*.md")):
        try:
            content = f.read_text()
        except OSError:
            continue
        score = sum(1 for k in keywords if k in content.lower())
        if score > 0:
            results.append((score, f, content))

    if not results:
        return f"No memory files matched: {query}"

    results.sort(key=lambda x: x[0], reverse=True)
    output = []
    for score, f, content in results[:max_results]:
        preview = content[:500].strip()
        output.append(f"### {f.stem}\n{preview}\n...")
    return "\n\n".join(output)
