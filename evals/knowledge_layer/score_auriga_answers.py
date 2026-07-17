#!/usr/bin/env python3
"""Score agent answers against auriga_web_qa.md Accept / Answer fields.

Usage:
  uv run python evals/knowledge_layer/score_auriga_answers.py \\
    --answers /path/to/auriga-web-answers.md \\
    [--qa-file evals/knowledge_layer/auriga_web_qa.md] \\
    [--ids Q06,Q07,Q13 | --all]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

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

_Q_HEADER = re.compile(r"^###\s+(Q\d+)\s*$", re.MULTILINE)
_FIELD = re.compile(
    r"^-\s+\*\*(Question|Answer|Evidence|Accept|Bucket):\*\*\s*(.+)$",
    re.MULTILINE,
)
# Plain "Q01: answer" lines
_ANSWER_LINE = re.compile(r"^(Q\d+)\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
# Markdown headings: "## Q01: answer" or "### Q01: answer"
_ANSWER_MD_COLON = re.compile(
    r"^#+\s*(Q\d+)\s*:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
# "## Q01 — Title\n\nbody..." blocks
_ANSWER_HEADING = re.compile(
    r"^##\s+(Q\d+)\s*[—–-]\s*.+?\n+(.*?)(?=^##\s+Q\d+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_EMPHASIS_RE = re.compile(r"(\*\*|__)(.+?)\1|(\*|_)(.+?)\3")


def parse_qa(text: str) -> dict[str, dict[str, str]]:
    parts = _Q_HEADER.split(text)
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


def parse_answers(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ANSWER_LINE.finditer(text):
        out[m.group(1).upper()] = m.group(2).strip()
    for m in _ANSWER_MD_COLON.finditer(text):
        qid = m.group(1).upper()
        body = m.group(2).strip()
        if qid not in out or len(body) > len(out[qid]):
            out[qid] = body
    for m in _ANSWER_HEADING.finditer(text):
        qid = m.group(1).upper()
        body = m.group(2).strip()
        if qid not in out or len(body) > len(out[qid]):
            out[qid] = body
    return out


def strip_markdown_emphasis(text: str) -> str:
    """Remove bold/italic markers so ``**15**`` still matches Accept ``15``."""

    def _repl(m: re.Match[str]) -> str:
        return m.group(2) or m.group(4) or ""

    prev = None
    out = text
    while prev != out:
        prev = out
        out = _EMPHASIS_RE.sub(_repl, out)
    return out


def _tokens_in(fragment: str) -> list[str]:
    found = re.findall(r"`([^`]+)`", fragment)
    if found:
        return found
    cleaned = fragment.strip().strip("`").strip("()").strip()
    return [cleaned] if cleaned else []


def _split_top_level(expr: str, op: str) -> list[str]:
    """Split on ``op`` (AND/OR) only at parenthesis depth 0."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    upper = expr.upper()
    token = f" {op.upper()} "
    while i < len(expr):
        ch = expr[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0 and upper.startswith(token, i):
            parts.append("".join(buf).strip())
            buf = []
            i += len(token)
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts if len(parts) > 1 else [expr.strip()]


def eval_accept_expr(expr: str, hay: str) -> bool:
    """Evaluate Accept expressions with nested AND/OR and parentheses."""
    expr = expr.strip()
    if not expr:
        return True

    # Strip one layer of wrapping parens
    if expr.startswith("(") and expr.endswith(")"):
        depth = 0
        wrapped = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    wrapped = False
                    break
        if wrapped and depth == 0:
            return eval_accept_expr(expr[1:-1], hay)

    or_parts = _split_top_level(expr, "OR")
    if len(or_parts) > 1:
        return any(eval_accept_expr(p, hay) for p in or_parts)

    and_parts = _split_top_level(expr, "AND")
    if len(and_parts) > 1:
        return all(eval_accept_expr(p, hay) for p in and_parts)

    toks = _tokens_in(expr)
    if not toks:
        # Comma-separated bare tokens
        toks = [c.strip().strip("`") for c in expr.split(",") if c.strip()]
    return bool(toks) and all(t.lower() in hay for t in toks)


def _accept_clauses(accept_field: str) -> list[str]:
    """Split Accept into AND-clauses (semicolon-separated top-level requirements)."""
    raw = accept_field.strip()
    if not raw:
        return []
    if ";" in raw:
        return [c.strip() for c in raw.split(";") if c.strip()]
    # Mixed comma / OR without semicolon (e.g. Q30): each comma piece is an AND clause
    if "," in raw and re.search(r"\s+OR\s+", raw, flags=re.IGNORECASE):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if "," in raw and "`" in raw and not re.search(r"\s+OR\s+", raw, flags=re.IGNORECASE):
        # All backtick tokens required — keep as one clause so tokenizer handles it
        return [raw]
    return [raw]


def score_one(answer_text: str, fields: dict[str, str]) -> tuple[bool, str]:
    hay = strip_markdown_emphasis(answer_text).lower()
    accept = fields.get("accept", "")
    clauses = _accept_clauses(accept)
    if clauses:
        for clause in clauses:
            if not eval_accept_expr(clause, hay):
                return False, f"missing Accept in clause {clause!r}"
        return True, "accept"
    gold = fields.get("answer", "").strip()
    if gold and gold[:40].lower() in hay:
        return True, "answer-prefix"
    words = [
        w
        for w in re.findall(r"[A-Za-z0-9_/.-]{4,}", gold)
        if w.lower() not in {"that", "with", "from", "this"}
    ]
    hits = sum(1 for w in words[:8] if w.lower() in hay)
    if hits >= 2:
        return True, f"answer-overlap({hits})"
    return False, "no match"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument(
        "--qa-file",
        type=Path,
        default=Path(__file__).resolve().parent / "auriga_web_qa.md",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=",".join(_HARD_SUBSET),
        help="Comma-separated question ids (default: hard subset)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Score all questions in the QA file (overrides --ids)",
    )
    args = parser.parse_args(argv)

    if not args.answers.is_file():
        print(f"answers file not found: {args.answers}", file=sys.stderr)
        return 1
    if not args.qa_file.is_file():
        print(f"QA file not found: {args.qa_file}", file=sys.stderr)
        return 1

    qa = parse_qa(args.qa_file.read_text(encoding="utf-8"))
    answers = parse_answers(args.answers.read_text(encoding="utf-8"))
    if args.all:
        wanted = sorted(qa.keys(), key=lambda q: int(q[1:]))
    else:
        wanted = [x.strip().upper() for x in args.ids.split(",") if x.strip()]

    passed = 0
    total = 0
    for qid in wanted:
        total += 1
        fields = qa.get(qid)
        if not fields:
            print(f"{qid}: SKIP (no ground truth)")
            continue
        ans = answers.get(qid, "")
        if not ans:
            print(f"{qid}: FAIL (no answer in transcript)")
            continue
        ok, reason = score_one(ans, fields)
        if ok:
            passed += 1
            print(f"{qid}: PASS ({reason})")
        else:
            print(f"{qid}: FAIL ({reason})")
            print(f"  got: {ans[:120]}")

    print(f"\nScore: {passed} / {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
