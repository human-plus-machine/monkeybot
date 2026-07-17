#!/usr/bin/env python3
"""Compute Recall@k / MRR / verification rate from a MonkeyBot transcript.

Usage:
  uv run python evals/knowledge_layer/recall_rank_report.py \\
    --transcript /path/to/transcript.ndjson \\
    [--qa-file evals/knowledge_layer/auriga_web_qa.md] \\
    [--verify-window 8]

Accepts a transcript.ndjson path or a session directory containing it.
For each QA Evidence path, finds the best (lowest) rank at which that file
appeared in any ``recall`` ToolCallResult hit list. Emits Recall@1/@3/@5/@10,
MRR, and verification rate (``read_file`` of a recall hit within N following
events). Also summarizes endurance signals (recall half-split, summarizations,
read_file thrash).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Reuse QA parser from the answer scorer
_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from score_auriga_answers import parse_qa  # noqa: E402

_INF = math.inf
# F21: tool renamed `recall` → `search`; count both so old transcripts still parse.
_RECALL_TOOL_NAMES = frozenset({"search", "recall"})
_EVIDENCE_PATH_RE = re.compile(r"`([^`]+)`")
_PATH_LINE_RE = re.compile(r"^([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)")


def load_ndjson(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def resolve_transcript_path(raw: Path) -> Path:
    if raw.is_file():
        return raw
    candidate = raw / "transcript.ndjson"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"No transcript.ndjson at {raw}")


def evidence_path(fields: dict[str, str]) -> str | None:
    raw = fields.get("evidence", "") or ""
    m = _EVIDENCE_PATH_RE.search(raw)
    if m:
        path = m.group(1).strip()
        # Drop trailing line refs accidentally inside ticks
        path = re.split(r"\s+", path, maxsplit=1)[0]
        return path.strip("`")
    m = _PATH_LINE_RE.search(raw.strip())
    return m.group(1) if m else None


def normalize_path(path: str) -> str:
    p = path.strip().lstrip("./").replace("\\", "/")
    # Strip common clone prefixes
    for prefix in ("auriga-web/", "workspace/", "repo/"):
        if p.startswith(prefix):
            p = p[len(prefix) :]
    return p


def path_matches(hit_path: str, evidence: str) -> bool:
    h = normalize_path(hit_path).lower()
    e = normalize_path(evidence).lower()
    if not h or not e:
        return False
    return h == e or h.endswith("/" + e) or e.endswith("/" + h) or h.endswith(e)


def parse_recall_hits(result_text: str) -> list[str]:
    """Extract ordered hit paths from a recall tool result payload."""
    text = (result_text or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Sometimes wrapped / prefixed
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []
    hits = payload.get("hits") or []
    paths: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        path = hit.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        key = normalize_path(path).lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(path.strip())
    return paths


def best_evidence_ranks(
    records: list[dict[str, Any]],
    qa: dict[str, dict[str, str]],
) -> dict[str, float]:
    """Map Qid → best recall rank for its Evidence path (inf if never hit)."""
    best: dict[str, float] = {}
    evidence_by_q: dict[str, str] = {}
    for qid, fields in qa.items():
        ep = evidence_path(fields)
        if ep:
            evidence_by_q[qid] = ep
            best[qid] = _INF

    for rec in records:
        if rec.get("type") != "ToolCallResult":
            continue
        if rec.get("tool") not in _RECALL_TOOL_NAMES:
            continue
        if rec.get("error"):
            continue
        paths = parse_recall_hits(str(rec.get("result") or ""))
        for qid, evidence in evidence_by_q.items():
            for rank, path in enumerate(paths, start=1):
                if path_matches(path, evidence):
                    best[qid] = min(best[qid], float(rank))
                    break
    return best


def recall_at_k(ranks: dict[str, float], k: int) -> float:
    if not ranks:
        return 0.0
    hits = sum(1 for r in ranks.values() if r <= k)
    return hits / len(ranks)


def mean_reciprocal_rank(ranks: dict[str, float]) -> float:
    if not ranks:
        return 0.0
    total = 0.0
    for r in ranks.values():
        if r < _INF:
            total += 1.0 / r
    return total / len(ranks)


def verification_rate(
    records: list[dict[str, Any]],
    *,
    window: int = 8,
) -> tuple[float, int, int]:
    """Fraction of recall calls followed by read_file of a hit path within ``window`` events."""
    verified = 0
    total = 0
    for i, rec in enumerate(records):
        if rec.get("type") != "ToolCallResult" or rec.get("tool") not in _RECALL_TOOL_NAMES:
            continue
        if rec.get("error"):
            continue
        paths = parse_recall_hits(str(rec.get("result") or ""))
        if not paths:
            continue
        total += 1
        hit_set = {normalize_path(p).lower() for p in paths}
        end = min(len(records), i + 1 + max(1, window))
        for nxt in records[i + 1 : end]:
            if nxt.get("type") not in {"ToolCallStarted", "ToolCallResult"}:
                continue
            if nxt.get("tool") != "read_file":
                continue
            args = nxt.get("args") if isinstance(nxt.get("args"), dict) else {}
            path = args.get("path") if isinstance(args, dict) else None
            if not path and nxt.get("type") == "ToolCallResult":
                # fall back: some transcripts only have path in started event
                continue
            if isinstance(path, str) and normalize_path(path).lower() in hit_set:
                verified += 1
                break
            # Also match suffix against hit paths
            if isinstance(path, str):
                np = normalize_path(path).lower()
                if any(path_matches(h, np) for h in hit_set):
                    verified += 1
                    break
    rate = (verified / total) if total else 0.0
    return rate, verified, total


def endurance_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize full-48 endurance signals from the transcript."""
    recall_seqs: list[int] = []
    for rec in records:
        if rec.get("type") == "ToolCallResult" and rec.get("tool") in _RECALL_TOOL_NAMES:
            seq = rec.get("seq")
            if isinstance(seq, int):
                recall_seqs.append(seq)

    summarizations = sum(1 for r in records if r.get("type") == "ContextSummarized")

    read_paths: list[str] = []
    for rec in records:
        if rec.get("type") != "ToolCallStarted" or rec.get("tool") != "read_file":
            continue
        args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
        path = args.get("path") if isinstance(args, dict) else None
        if isinstance(path, str) and path.strip():
            read_paths.append(normalize_path(path))

    counts = Counter(read_paths)
    thrash = {p: n for p, n in counts.items() if n >= 3}

    # Half-split by seq midpoint of the session
    seqs = [r.get("seq") for r in records if isinstance(r.get("seq"), int)]
    mid = (min(seqs) + max(seqs)) / 2 if seqs else 0
    first_half = sum(1 for s in recall_seqs if s <= mid)
    second_half = sum(1 for s in recall_seqs if s > mid)

    return {
        "recall_total": len(recall_seqs),
        "recall_first_half": first_half,
        "recall_second_half": second_half,
        "last_recall_seq": max(recall_seqs) if recall_seqs else None,
        "summarizations": summarizations,
        "read_file_thrash_paths": len(thrash),
        "thrash_examples": sorted(thrash.items(), key=lambda x: -x[1])[:5],
    }


def format_report(
    ranks: dict[str, float],
    *,
    verify: tuple[float, int, int],
    endurance: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("## Recall rank report")
    lines.append("")
    n = len(ranks)
    lines.append(f"Questions with Evidence: {n}")
    if n:
        lines.append(
            f"Recall@1={recall_at_k(ranks, 1):.3f}  "
            f"@3={recall_at_k(ranks, 3):.3f}  "
            f"@5={recall_at_k(ranks, 5):.3f}  "
            f"@10={recall_at_k(ranks, 10):.3f}"
        )
        lines.append(f"MRR={mean_reciprocal_rank(ranks):.3f}")
    rate, verified, total = verify
    lines.append(f"Verification rate={rate:.3f} ({verified}/{total} recalls followed by read_file of a hit)")
    lines.append("")
    lines.append("### Per-question best evidence rank")
    for qid in sorted(ranks.keys(), key=lambda q: int(q[1:])):
        r = ranks[qid]
        label = "∞" if r >= _INF else str(int(r))
        lines.append(f"- {qid}: rank {label}")
    lines.append("")
    lines.append("### Endurance")
    lines.append(
        f"- recall calls: {endurance['recall_total']} "
        f"(first-half seqs {endurance['recall_first_half']}, "
        f"second-half {endurance['recall_second_half']})"
    )
    lines.append(f"- last recall seq: {endurance['last_recall_seq']}")
    lines.append(f"- ContextSummarized: {endurance['summarizations']}")
    lines.append(
        f"- read_file thrash paths (≥3 reads): {endurance['read_file_thrash_paths']}"
    )
    for path, n in endurance.get("thrash_examples") or []:
        lines.append(f"  - {path}: {n}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="Path to transcript.ndjson or a session directory",
    )
    parser.add_argument(
        "--qa-file",
        type=Path,
        default=_EVAL_DIR / "auriga_web_qa.md",
    )
    parser.add_argument(
        "--verify-window",
        type=int,
        default=8,
        help="Max following events to look for read_file verification",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default="",
        help="Optional comma-separated Q ids (default: all with Evidence)",
    )
    args = parser.parse_args(argv)

    try:
        tpath = resolve_transcript_path(args.transcript)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not args.qa_file.is_file():
        print(f"QA file not found: {args.qa_file}", file=sys.stderr)
        return 1

    records = load_ndjson(tpath)
    qa = parse_qa(args.qa_file.read_text(encoding="utf-8"))
    if args.ids.strip():
        wanted = {x.strip().upper() for x in args.ids.split(",") if x.strip()}
        qa = {k: v for k, v in qa.items() if k in wanted}

    ranks = best_evidence_ranks(records, qa)
    verify = verification_rate(records, window=args.verify_window)
    endurance = endurance_stats(records)
    print(format_report(ranks, verify=verify, endurance=endurance))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
