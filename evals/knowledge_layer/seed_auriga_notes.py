#!/usr/bin/env python3
"""Seed `.monkeybot/knowledge/notes/` from auriga_web_qa.md Evidence fields.

Config B notes-heavy slice: curated notes with ``[[workspace:…]]`` links into
an auriga-web workspace tree so ``recall`` can exercise graph expansion.

Usage:
  uv run python evals/knowledge_layer/seed_auriga_notes.py \\
    --agent-root /path/to/test_bot \\
    [--qa-file evals/knowledge_layer/auriga_web_qa.md] \\
    [--ids Q06,Q07,Q13]

Protocol for Config B hard-subset run (compare to Config A in design doc):
  1. Clone auriga-web into ``<agent>/workspace/auriga-web`` (or workspace root).
  2. Run this seed so notes land under ``<workspace>/.monkeybot/knowledge/notes/``.
  3. Ensure ``knowledge.enabled: true`` in monkeybot.yaml.
  4. Ask the hard subset (Q06, Q07, Q13, Q14, Q19, Q29, Q30, Q40, Q42, Q48)
     using the questions-only pack; prefer ``recall`` then ``read_file``.
  5. Score with ``score_auriga_answers.py`` against Accept fields.
  6. Log results next to the Config A baseline in docs/workspace-index-design.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_Q_HEADER = re.compile(r"^###\s+(Q\d+)\s*$", re.MULTILINE)
_FIELD = re.compile(
    r"^-\s+\*\*(Question|Answer|Evidence|Accept|Bucket):\*\*\s*(.+)$",
    re.MULTILINE,
)
_EVIDENCE_PATH = re.compile(
    r"`([^`]+)`|(?:^|,|;)\s*([A-Za-z0-9_./\[\]-]+\.[A-Za-z0-9]+)",
)
_LINE_SPAN = re.compile(
    r"(?:L|~L)?(\d+)\s*[–-]\s*(?:L)?(\d+)|(?:L|~L)(\d+)",
    re.IGNORECASE,
)

_HARD_SUBSET = (
    "Q06",
    "Q07",
    "Q13",
    "Q14",
    "Q19",
    "Q29",
    "Q30",
    "Q40",
    "Q42",
    "Q48",
)


def parse_qa(text: str) -> dict[str, dict[str, str]]:
    """Return ``{Qxx: {question, answer, evidence, accept, bucket}}``."""
    parts = _Q_HEADER.split(text)
    # parts: [preamble, id1, body1, id2, body2, ...]
    out: dict[str, dict[str, str]] = {}
    for i in range(1, len(parts), 2):
        qid = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        fields: dict[str, str] = {}
        for m in _FIELD.finditer(body):
            fields[m.group(1).lower()] = m.group(2).strip()
        if fields:
            out[qid] = fields
    return out


def evidence_to_wiki_links(evidence: str, *, repo_prefix: str = "auriga-web") -> list[str]:
    """Turn an Evidence field into ``[[workspace:…]]`` links."""
    links: list[str] = []
    # Split on `;` for multiple evidence refs
    for chunk in re.split(r";", evidence):
        chunk = chunk.strip()
        if not chunk:
            continue
        path = None
        m = re.search(r"`([^`]+)`", chunk)
        if m:
            path = m.group(1).strip()
        else:
            m2 = re.search(r"([A-Za-z0-9_./\[\]-]+\.[A-Za-z0-9]+)", chunk)
            if m2:
                path = m2.group(1).strip()
        if not path:
            continue
        # Normalize path (drop leading ./)
        path = path.lstrip("./")
        if repo_prefix and not path.startswith(repo_prefix + "/"):
            # Agent workspace often has clone at auriga-web/
            path = f"{repo_prefix}/{path}"

        start = end = None
        for sm in _LINE_SPAN.finditer(chunk):
            if sm.group(1) and sm.group(2):
                start, end = int(sm.group(1)), int(sm.group(2))
            elif sm.group(3):
                start = end = int(sm.group(3))
            break
        if start is not None:
            links.append(f"[[workspace:{path}#L{start}-{end}]]")
        else:
            links.append(f"[[workspace:{path}]]")
    return links


def note_body(qid: str, fields: dict[str, str], links: list[str]) -> str:
    question = fields.get("question", "")
    answer = fields.get("answer", "")
    bucket = fields.get("bucket", "")
    link_block = "\n".join(links) if links else "_no workspace links parsed_"
    return (
        f"# {qid}\n\n"
        f"**Bucket:** {bucket}\n\n"
        f"## Topic\n\n{question}\n\n"
        f"## Curated summary\n\n{answer}\n\n"
        f"## Workspace links\n\n{link_block}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-root",
        type=Path,
        required=True,
        help="Agent root (contains workspace/ and optionally monkeybot_config/).",
    )
    parser.add_argument(
        "--qa-file",
        type=Path,
        default=None,
        help="Path to auriga_web_qa.md (default: beside this script).",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=",".join(_HARD_SUBSET),
        help="Comma-separated question ids to seed (default: hard subset).",
    )
    parser.add_argument(
        "--repo-prefix",
        type=str,
        default="auriga-web",
        help="Workspace-relative prefix for cloned repo paths in wiki links.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print note paths without writing.",
    )
    args = parser.parse_args(argv)

    qa_path = args.qa_file or (Path(__file__).resolve().parent / "auriga_web_qa.md")
    if not qa_path.is_file():
        print(f"QA file not found: {qa_path}", file=sys.stderr)
        return 1

    wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
    qa = parse_qa(qa_path.read_text(encoding="utf-8"))

    # Notes live under workspace/.monkeybot/knowledge/notes (indexer default)
    workspace = args.agent_root / "workspace"
    notes_dir = workspace / ".monkeybot" / "knowledge" / "notes"
    if not args.dry_run:
        notes_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for qid in sorted(wanted):
        fields = qa.get(qid)
        if not fields:
            print(f"skip missing {qid}", file=sys.stderr)
            continue
        links = evidence_to_wiki_links(
            fields.get("evidence", ""), repo_prefix=args.repo_prefix
        )
        body = note_body(qid, fields, links)
        out_path = notes_dir / f"{qid.lower()}-note.md"
        print(f"{out_path} ({len(links)} links)")
        if not args.dry_run:
            out_path.write_text(body, encoding="utf-8")
        written += 1

    print(f"seeded {written} notes → {notes_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
