"""Offline analysis of session transcripts for harness debugging.

Reads ``transcript.ndjson`` and writes sibling ``brief.md``, ``report.json``,
and ``meta.json``. Invoked from gateway session teardown — not agent-facing.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from monkeybot import __version__ as monkeybot_version
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import DURABLE_EVENT_KINDS

logger = logging.getLogger(__name__)

_BRIEF_NAME = "brief.md"
_REPORT_NAME = "report.json"
_META_NAME = "meta.json"
_TOOL_THRASH_MIN = 3
_SNIPPET_LEN = 120
_POLICY_HINTS = ("allowlist", "denied", "not allowed", "blocked", "policy", "security")
_SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}

_TIMELINE_TYPES: frozenset[str] = frozenset(
    {
        "SessionManifest",
        "UserMessage",
        "ProviderRequest",
        "ProviderResponse",
        *DURABLE_EVENT_KINDS,
    }
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _ms_between(a: str | None, b: str | None) -> int | None:
    da, db = _parse_ts(a), _parse_ts(b)
    if da is None or db is None:
        return None
    return max(0, int((db - da).total_seconds() * 1000))


def _snippet(value: Any, limit: int = _SNIPPET_LEN) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _args_fingerprint(args: Any) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _schema_chars(tools: list[Any] | None) -> int:
    if not tools:
        return 0
    try:
        return len(json.dumps(tools, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def _ts(rec: dict[str, Any] | None) -> str | None:
    if not rec:
        return None
    ts = rec.get("ts")
    return ts if isinstance(ts, str) else None


def _evidence(rec: dict[str, Any], snippet: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"seq": rec.get("seq"), "type": rec.get("type")}
    if snippet is not None:
        out["snippet"] = snippet
    return out


def _timeline_entry(rec: dict[str, Any], rtype: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "seq": rec.get("seq"),
        "ts": rec.get("ts") or rec.get("started_at"),
        "type": rtype,
        "request_id": rec.get("request_id"),
    }
    if rtype == "UserMessage":
        entry["snippet"] = _snippet(rec.get("content"), 80)
    elif rtype in ("ProviderResponse", "AssistantTextEnded"):
        entry["snippet"] = _snippet(rec.get("text"), 80)
        if rtype == "ProviderResponse":
            entry["tool_request_count"] = len(rec.get("tool_requests") or [])
    elif rtype == "ToolCallStarted":
        entry["tool"] = rec.get("tool")
    elif rtype == "ToolCallResult":
        entry["tool"] = rec.get("tool")
        entry["ok"] = not bool(rec.get("error"))
    elif rtype == "Error":
        entry["snippet"] = _snippet(rec.get("error"), 80)
    elif rtype == "ContextEpochStarted":
        entry["snippet"] = _snippet(
            {"epoch_id": rec.get("epoch_id"), "changed_sources": rec.get("changed_sources")},
            80,
        )
    elif rtype == "ContextSummarized":
        entry["snippet"] = _snippet(rec.get("summary") or rec.get("reason"), 80)
    elif rtype == "ThinkingBlockComplete":
        entry["snippet"] = "(thinking complete)"
    elif rtype == "RedactedThinkingBlock":
        entry["snippet"] = "(redacted thinking)"
    return entry


def load_ndjson(path: Path) -> list[dict[str, Any]]:
    """Load NDJSON records; skip blank / corrupt lines."""
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("transcript read failed %s", kv(path=path), exc_info=True)
        return records
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("skipping corrupt NDJSON line %s", kv(line=line_no, path=path))
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def reconstruct_messages(records: list[dict[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Rebuild full message lists from delta-encoded ProviderRequest records."""
    rebuilt: dict[tuple[str, int], list[dict[str, Any]]] = {}
    state_by_request: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("type") != "ProviderRequest":
            continue
        request_id = str(rec.get("request_id") or "")
        inner_turn = int(rec.get("inner_turn") or 0)
        delta = list(rec.get("messages") or [])
        offset = int(rec.get("message_offset") or 0)
        reset = bool(rec.get("messages_reset"))
        if reset or request_id not in state_by_request:
            state_by_request[request_id] = list(delta)
        else:
            prev = state_by_request[request_id]
            state_by_request[request_id] = (
                prev[:offset] + list(delta) if offset <= len(prev) else prev + list(delta)
            )
        rebuilt[(request_id, inner_turn)] = list(state_by_request[request_id])
    return rebuilt


@dataclass
class Finding:
    kind: str
    severity: str  # info | warn | error
    summary: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
        }


@dataclass
class SessionAnalysis:
    session_id: str
    started_at: str | None
    model: str | None
    provider: str | None
    workspace_root: str | None
    turn_count: int
    inner_turn_count: int
    error_count: int
    cancelled_turn_count: int
    token_totals: dict[str, int]
    wall_times_ms: list[dict[str, Any]]
    llm_latencies_ms: list[dict[str, Any]]
    tool_latencies_ms: list[dict[str, Any]]
    tool_schema_sizes: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    findings: list[Finding]

    def scorecard(self) -> dict[str, Any]:
        def by_dur(item: dict[str, Any]) -> int:
            return int(item.get("duration_ms") or 0)

        return {
            "turn_count": self.turn_count,
            "inner_turn_count": self.inner_turn_count,
            "error_count": self.error_count,
            "cancelled_turn_count": self.cancelled_turn_count,
            "error_rate": (self.error_count / self.turn_count) if self.turn_count else 0.0,
            "token_totals": self.token_totals,
            "time_in_llm_ms": sum(by_dur(x) for x in self.llm_latencies_ms),
            "time_in_tools_ms": sum(by_dur(x) for x in self.tool_latencies_ms),
            "slowest_turns": sorted(self.wall_times_ms, key=by_dur, reverse=True)[:5],
            "slowest_llm_calls": sorted(self.llm_latencies_ms, key=by_dur, reverse=True)[:5],
            "slowest_tools": sorted(self.tool_latencies_ms, key=by_dur, reverse=True)[:5],
            "tool_schema_sizes": self.tool_schema_sizes[:10],
            "finding_count": len(self.findings),
        }


def _records_by_type(records: list[dict[str, Any]], type_name: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("type") == type_name]


def _wall_times(
    user_msgs: list[dict[str, Any]],
    turn_completes: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    tc_by_req = {str(r.get("request_id")): r for r in turn_completes}
    err_reqs = {str(r.get("request_id")) for r in errors}
    wall_times: list[dict[str, Any]] = []
    cancelled = 0
    for um in user_msgs:
        rid = str(um.get("request_id") or "")
        failed = rid in err_reqs
        if failed:
            cancelled += 1
        wall_times.append(
            {
                "request_id": rid,
                "duration_ms": _ms_between(_ts(um), _ts(tc_by_req.get(rid))),
                "status": "error" if failed else "ok",
                "seq": um.get("seq"),
            }
        )
    return wall_times, cancelled


def _llm_latencies(
    provider_reqs: list[dict[str, Any]],
    provider_resps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resp_by_key = {
        (str(r.get("request_id")), int(r.get("inner_turn") or 0)): r for r in provider_resps
    }
    out: list[dict[str, Any]] = []
    for req in provider_reqs:
        rid = str(req.get("request_id") or "")
        inner = int(req.get("inner_turn") or 0)
        out.append(
            {
                "request_id": rid,
                "inner_turn": inner,
                "duration_ms": _ms_between(_ts(req), _ts(resp_by_key.get((rid, inner)))),
                "seq": req.get("seq"),
            }
        )
    return out


def _tool_latencies(
    tool_starts: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    starts_by_call = {
        str(s.get("call_id")): s for s in tool_starts if s.get("call_id")
    }
    results_by_call: dict[str, dict[str, Any]] = {}
    latencies: list[dict[str, Any]] = []
    for res in tool_results:
        cid = str(res.get("call_id") or "")
        if cid:
            results_by_call[cid] = res
        start = starts_by_call.get(cid)
        latencies.append(
            {
                "call_id": cid,
                "tool": res.get("tool") or (start.get("tool") if start else None),
                "duration_ms": _ms_between(_ts(start), _ts(res)),
                "seq": res.get("seq"),
                "error": res.get("error"),
            }
        )
    return latencies, results_by_call


def _tool_schema_sizes(provider_reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sizes: list[dict[str, Any]] = []
    for req in provider_reqs:
        tools = req.get("tools")
        if tools is None:
            continue
        tool_list = tools if isinstance(tools, list) else []
        sizes.append(
            {
                "type": "ProviderRequest",
                "request_id": req.get("request_id"),
                "inner_turn": req.get("inner_turn"),
                "tool_count": len(tool_list),
                "schema_chars": _schema_chars(tool_list),
                "seq": req.get("seq"),
            }
        )
    return sizes


def _token_totals(provider_resps: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    for resp in provider_resps:
        usage = resp.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        for key in totals:
            raw = usage.get(key) or 0
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
                totals[key] += int(raw)
    return totals


def _build_timeline(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for rec in records:
        rtype = rec.get("type")
        if isinstance(rtype, str) and rtype in _TIMELINE_TYPES:
            timeline.append(_timeline_entry(rec, rtype))
    return timeline


def _smell_tool_thrash(tool_starts: list[dict[str, Any]]) -> list[Finding]:
    key_evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    call_keys: list[tuple[str, str]] = []
    for s in tool_starts:
        tool = str(s.get("tool") or "")
        key = (tool, _args_fingerprint(s.get("args")))
        call_keys.append(key)
        key_evidence[key].append(_evidence(s, _snippet({"tool": tool, "args": s.get("args")})))
    findings: list[Finding] = []
    for key, n in Counter(call_keys).items():
        if n < _TOOL_THRASH_MIN:
            continue
        tool, _fp = key
        findings.append(
            Finding(
                kind="tool_loop_thrash",
                severity="warn",
                summary=f"Tool {tool!r} called {n} times with similar args",
                evidence=key_evidence[key][:5],
            )
        )
    return findings


def _smell_empty_post_tool(provider_resps: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for i, resp in enumerate(provider_resps):
        text = (resp.get("text") or "").strip() if isinstance(resp.get("text"), str) else ""
        tool_reqs = resp.get("tool_requests") or []
        inner = int(resp.get("inner_turn") or 0)
        prior_had_tools = False
        if i > 0:
            prev = provider_resps[i - 1]
            if str(prev.get("request_id")) == str(resp.get("request_id")) and (
                prev.get("tool_requests") or []
            ):
                prior_had_tools = True
        if text or tool_reqs or not (inner > 1 or prior_had_tools):
            continue
        findings.append(
            Finding(
                kind="empty_post_tool_reply",
                severity="warn",
                summary=(
                    f"Empty ProviderResponse after tools "
                    f"(request {resp.get('request_id')}, inner {inner})"
                ),
                evidence=[_evidence(resp, "(empty text)")],
            )
        )
    return findings


def _smell_orphan_tools(
    provider_resps: list[dict[str, Any]],
    tool_starts: list[dict[str, Any]],
    results_by_call: dict[str, dict[str, Any]],
) -> list[Finding]:
    orphan_evidence: list[dict[str, Any]] = []
    for resp in provider_resps:
        for tr in resp.get("tool_requests") or []:
            if not isinstance(tr, dict):
                continue
            cid = str(tr.get("call_id") or tr.get("id") or "")
            if cid and cid not in results_by_call:
                orphan_evidence.append(
                    _evidence(
                        resp,
                        _snippet({"call_id": cid, "name": tr.get("name") or tr.get("tool")}),
                    )
                )
    for s in tool_starts:
        cid = str(s.get("call_id") or "")
        if cid and cid not in results_by_call:
            orphan_evidence.append(_evidence(s, _snippet({"call_id": cid, "tool": s.get("tool")})))
    if not orphan_evidence:
        return []
    return [
        Finding(
            kind="tool_call_orphans",
            severity="error",
            summary=f"{len(orphan_evidence)} tool call(s) without matching ToolCallResult",
            evidence=orphan_evidence[:8],
        )
    ]


def _smell_policy(tool_results: list[dict[str, Any]]) -> list[Finding]:
    policy_ev = []
    for res in tool_results:
        err = res.get("error")
        if isinstance(err, str) and err and any(h in err.lower() for h in _POLICY_HINTS):
            policy_ev.append(_evidence(res, _snippet(err)))
    if not policy_ev:
        return []
    return [
        Finding(
            kind="policy_vs_execution",
            severity="warn",
            summary=f"{len(policy_ev)} tool result(s) look like policy/allowlist denials",
            evidence=policy_ev[:8],
        )
    ]


def _smell_context_churn(
    provider_reqs: list[dict[str, Any]],
    context_summarized: list[dict[str, Any]],
) -> list[Finding]:
    resets = [r for r in provider_reqs if r.get("messages_reset")]
    if len(context_summarized) < 2 and len(resets) < 2:
        return []
    ev = [_evidence(r) for r in context_summarized[:4]] + [
        _evidence(r, "messages_reset") for r in resets[:4]
    ]
    return [
        Finding(
            kind="context_churn",
            severity="info",
            summary=(
                f"Context churn: {len(context_summarized)} ContextSummarized, "
                f"{len(resets)} messages_reset"
            ),
            evidence=ev,
        )
    ]


def _smell_progressive_tools(tool_schema_sizes: list[dict[str, Any]]) -> list[Finding]:
    sizes = [(s.get("tool_count") or 0, s) for s in tool_schema_sizes]
    if len(sizes) < 2:
        return []
    counts_only = [c for c, _ in sizes]
    if max(counts_only) - min(counts_only) < 3:
        return []
    return [
        Finding(
            kind="progressive_tools",
            severity="info",
            summary=(
                f"Tool list size changed mid-session ({min(counts_only)} → {max(counts_only)})"
            ),
            evidence=[_evidence(s, f"tools={c}") for c, s in sizes[:6]],
        )
    ]


def _smell_token_waste(
    tool_schema_sizes: list[dict[str, Any]],
    user_msgs: list[dict[str, Any]],
) -> list[Finding]:
    max_schema = max((s.get("schema_chars") or 0 for s in tool_schema_sizes), default=0)
    user_chars = sum(len(str(u.get("content") or "")) for u in user_msgs)
    avg_user = (user_chars / len(user_msgs)) if user_msgs else 0
    if max_schema < 8000 or avg_user >= 200:
        return []
    big = max(tool_schema_sizes, key=lambda s: s.get("schema_chars") or 0)
    return [
        Finding(
            kind="token_waste",
            severity="warn",
            summary=(
                f"Large tool schemas ({max_schema} chars) vs short user turns "
                f"(avg {avg_user:.0f} chars)"
            ),
            evidence=[_evidence(big, f"schema_chars={max_schema}")],
        )
    ]


def _smell_error_clustering(
    errors: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> list[Finding]:
    err_counter: Counter[str] = Counter()
    err_ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in errors:
        msg = str(e.get("error") or "")
        err_counter[msg] += 1
        err_ev[msg].append(_evidence(e, _snippet(msg)))
    for tr in tool_results:
        tool_err = tr.get("error")
        if isinstance(tool_err, str) and tool_err:
            err_counter[tool_err] += 1
            err_ev[tool_err].append(_evidence(tr, _snippet(tool_err)))
    findings: list[Finding] = []
    for msg, n in err_counter.items():
        if n >= 2 and msg:
            findings.append(
                Finding(
                    kind="error_clustering",
                    severity="warn",
                    summary=f"Repeated error ({n}×): {_snippet(msg, 80)}",
                    evidence=err_ev[msg][:5],
                )
            )
    return findings


def _detect_smells(
    *,
    provider_reqs: list[dict[str, Any]],
    provider_resps: list[dict[str, Any]],
    tool_starts: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    results_by_call: dict[str, dict[str, Any]],
    context_summarized: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    tool_schema_sizes: list[dict[str, Any]],
    user_msgs: list[dict[str, Any]],
) -> list[Finding]:
    findings = (
        _smell_tool_thrash(tool_starts)
        + _smell_empty_post_tool(provider_resps)
        + _smell_orphan_tools(provider_resps, tool_starts, results_by_call)
        + _smell_policy(tool_results)
        + _smell_context_churn(provider_reqs, context_summarized)
        + _smell_progressive_tools(tool_schema_sizes)
        + _smell_token_waste(tool_schema_sizes, user_msgs)
        + _smell_error_clustering(errors, tool_results)
    )
    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 9), f.kind))
    return findings


def analyze_records(records: list[dict[str, Any]]) -> SessionAnalysis:
    """Build a structured analysis from already-loaded NDJSON records."""
    manifest = next((r for r in records if r.get("type") == "SessionManifest"), {})
    user_msgs = _records_by_type(records, "UserMessage")
    provider_reqs = _records_by_type(records, "ProviderRequest")
    provider_resps = _records_by_type(records, "ProviderResponse")
    turn_completes = _records_by_type(records, "TurnComplete")
    errors = _records_by_type(records, "Error")
    tool_starts = _records_by_type(records, "ToolCallStarted")
    tool_results = _records_by_type(records, "ToolCallResult")
    context_summarized = _records_by_type(records, "ContextSummarized")

    wall_times, cancelled = _wall_times(user_msgs, turn_completes, errors)
    tool_latencies, results_by_call = _tool_latencies(tool_starts, tool_results)
    tool_schema_sizes = _tool_schema_sizes(provider_reqs)

    return SessionAnalysis(
        session_id=str(manifest.get("session_id") or ""),
        started_at=manifest.get("started_at") if isinstance(manifest.get("started_at"), str) else None,
        model=manifest.get("model") if isinstance(manifest.get("model"), str) else None,
        provider=manifest.get("provider") if isinstance(manifest.get("provider"), str) else None,
        workspace_root=(
            manifest.get("workspace_root")
            if isinstance(manifest.get("workspace_root"), str)
            else None
        ),
        turn_count=len(user_msgs),
        inner_turn_count=len(provider_reqs),
        error_count=len(errors),
        cancelled_turn_count=cancelled,
        token_totals=_token_totals(provider_resps),
        wall_times_ms=wall_times,
        llm_latencies_ms=_llm_latencies(provider_reqs, provider_resps),
        tool_latencies_ms=tool_latencies,
        tool_schema_sizes=tool_schema_sizes,
        timeline=_build_timeline(records),
        findings=_detect_smells(
            provider_reqs=provider_reqs,
            provider_resps=provider_resps,
            tool_starts=tool_starts,
            tool_results=tool_results,
            results_by_call=results_by_call,
            context_summarized=context_summarized,
            errors=errors,
            tool_schema_sizes=tool_schema_sizes,
            user_msgs=user_msgs,
        ),
    )


def _brief_summary(analysis: SessionAnalysis, sc: dict[str, Any]) -> list[str]:
    lines = [
        "## Session summary",
        "",
        f"- **session_id:** `{analysis.session_id or '(unknown)'}`",
    ]
    if analysis.started_at:
        lines.append(f"- **started_at:** `{analysis.started_at}`")
    if analysis.model:
        lines.append(f"- **model:** `{analysis.model}`")
    if analysis.provider:
        lines.append(f"- **provider:** `{analysis.provider}`")
    tt = analysis.token_totals
    lines.extend(
        [
            f"- **user turns:** {analysis.turn_count}",
            f"- **inner LLM turns:** {analysis.inner_turn_count}",
            (
                f"- **errors:** {analysis.error_count} "
                f"(cancelled/failed turns: {analysis.cancelled_turn_count})"
            ),
            (
                f"- **tokens:** in={tt.get('input_tokens', 0)} out={tt.get('output_tokens', 0)} "
                f"cached={tt.get('cached_tokens', 0)}"
            ),
            f"- **time:** LLM≈{sc['time_in_llm_ms']}ms tools≈{sc['time_in_tools_ms']}ms",
            "",
        ]
    )
    return lines


def _brief_timeline(analysis: SessionAnalysis) -> list[str]:
    lines = ["## Timeline (compressed)", ""]
    for entry in analysis.timeline[:80]:
        seq = entry.get("seq")
        seq_s = f"seq={seq} " if seq is not None else ""
        bits = [f"- `{seq_s}{entry.get('type')}`"]
        if entry.get("request_id"):
            bits.append(f"req={entry['request_id']}")
        if entry.get("tool"):
            bits.append(f"tool={entry['tool']}")
        if entry.get("snippet"):
            bits.append(f"— {entry['snippet']}")
        lines.append(" ".join(bits))
    if len(analysis.timeline) > 80:
        lines.append(f"- … ({len(analysis.timeline) - 80} more events)")
    lines.append("")
    return lines


def _brief_perf(sc: dict[str, Any]) -> list[str]:
    lines = ["## Perf hotspots", ""]
    if sc["slowest_turns"]:
        lines.append("**Slowest user turns**")
        for t in sc["slowest_turns"][:5]:
            lines.append(
                f"- req `{t.get('request_id')}`: {t.get('duration_ms')}ms ({t.get('status')})"
            )
    if sc["slowest_llm_calls"]:
        lines.append("**Slowest LLM calls**")
        for t in sc["slowest_llm_calls"][:5]:
            lines.append(
                f"- req `{t.get('request_id')}` inner={t.get('inner_turn')}: {t.get('duration_ms')}ms"
            )
    if sc["slowest_tools"]:
        lines.append("**Slowest tools**")
        for t in sc["slowest_tools"][:5]:
            lines.append(
                f"- `{t.get('tool')}` call `{t.get('call_id')}`: {t.get('duration_ms')}ms"
            )
    if not (sc["slowest_turns"] or sc["slowest_llm_calls"] or sc["slowest_tools"]):
        lines.append("- (no timing data)")
    lines.append("")
    return lines


def _brief_findings(analysis: SessionAnalysis) -> list[str]:
    lines = ["## Suspected harness issues (ranked)", ""]
    if not analysis.findings:
        lines.append("- None detected by v1 heuristics.")
    else:
        for f in analysis.findings:
            lines.append(f"- **[{f.severity}] {f.kind}:** {f.summary}")
    lines.extend(["", "## Exact evidence (seq + type + snippet)", ""])
    if not analysis.findings:
        lines.append("- (none)")
    else:
        for f in analysis.findings:
            lines.append(f"### {f.kind}")
            for ev in f.evidence[:5]:
                lines.append(
                    f"- seq={ev.get('seq')} type={ev.get('type')} {_snippet(ev.get('snippet'), 100)}"
                )
    lines.append("")
    return lines


def _brief_probes(analysis: SessionAnalysis) -> list[str]:
    probes = [
        "`src/monkeybot/core/runtime/loop.py` — provider turn loop / TurnComplete timing",
        "`src/monkeybot/core/persistence/transcript.py` — what was captured",
        "`monkeybot_config/command_allowlist.yaml` — policy vs execution denials",
        "`runtime.transcript_enabled` / `MONKEYBOT_TRANSCRIPT_ENABLED`",
        "`evals/smoke_agent` — re-run a focused smoke after a harness patch",
    ]
    kinds = {f.kind for f in analysis.findings}
    if "token_waste" in kinds or "progressive_tools" in kinds:
        probes.insert(0, "`docs/progressive-mcp-tools.md` — tool-schema bloat / progressive disclosure")
    if "policy_vs_execution" in kinds:
        probes.insert(0, "Inspectors + allowlist — check ToolCallResult.error wording")
    lines = ["## Suggested next probes (files / config knobs)", ""]
    lines.extend(f"- {p}" for p in probes[:6])
    lines.append("")
    return lines


def render_brief(analysis: SessionAnalysis) -> str:
    """Render the agent/human-readable debug brief."""
    sc = analysis.scorecard()
    lines = (
        _brief_summary(analysis, sc)
        + _brief_timeline(analysis)
        + _brief_perf(sc)
        + _brief_findings(analysis)
        + _brief_probes(analysis)
    )
    return "\n".join(lines)


def write_report_artifacts(analysis: SessionAnalysis, session_dir: Path) -> None:
    """Write brief.md / report.json / meta.json into ``session_dir`` (overwrite)."""
    report = {
        "session_id": analysis.session_id,
        "started_at": analysis.started_at,
        "model": analysis.model,
        "provider": analysis.provider,
        "scorecard": analysis.scorecard(),
        "findings": [f.to_dict() for f in analysis.findings],
        "timeline": analysis.timeline,
    }
    meta = {
        "session_id": analysis.session_id,
        "started_at": analysis.started_at,
        "analyzed_at": _now_iso(),
        "harness": {"package": "monkeybot", "version": monkeybot_version},
        "workspace_root": analysis.workspace_root,
        "model": analysis.model,
        "provider": analysis.provider,
    }
    (session_dir / _BRIEF_NAME).write_text(render_brief(analysis), encoding="utf-8")
    (session_dir / _REPORT_NAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (session_dir / _META_NAME).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def analyze_transcript(ndjson_path: Path) -> Path | None:
    """Write brief.md / report.json / meta.json beside NDJSON. Returns session_dir or None."""
    path = Path(ndjson_path)
    if not path.is_file():
        logger.debug("transcript analyze skipped; missing file %s", kv(path=path))
        return None
    try:
        if path.stat().st_size == 0:
            logger.debug("transcript analyze skipped; empty file %s", kv(path=path))
            return None
    except OSError:
        logger.warning("transcript analyze stat failed %s", kv(path=path), exc_info=True)
        return None

    records = load_ndjson(path)
    if not records:
        logger.debug("transcript analyze skipped; no records %s", kv(path=path))
        return None

    try:
        analysis = analyze_records(records)
        write_report_artifacts(analysis, path.parent)
        return path.parent
    except Exception:
        logger.warning("transcript analyze failed %s", kv(path=path), exc_info=True)
        return None


__all__ = [
    "analyze_transcript",
    "analyze_records",
    "reconstruct_messages",
]
