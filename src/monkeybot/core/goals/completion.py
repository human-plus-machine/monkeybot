"""Evidence audit and completion judge for native goals.

A goal stays active until every requirement is proved. The deterministic audit
rejects anything that is not ``proved``. ``ProviderCompletionJudge`` then checks
that the audit covers the objective and that the cited evidence is in the
transcript. Timeout, unusable output, and a missing provider all fail closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.verifier.binding import (
    ModelRef,
    ProviderRef,
    resolve_live_model,
    resolve_live_provider,
)
from monkeybot.core.verifier.classify import collect_verifier_stream

logger = logging.getLogger(__name__)

AUDIT_VERDICTS = frozenset({"proved", "contradicted", "incomplete", "too_weak", "missing"})
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_TRANSCRIPT_MAX = 12_000
_NOTE_MAX = 480

_JUDGE_SYSTEM = """\
You are a completion auditor. A goal is complete only when current evidence \
proves every requirement of the objective. Stopping a turn, running out of time, \
or a plausible answer does not finish it.

Return ONLY compact JSON:
{"verdict":"pass|fail","uncovered_requirements":["..."],\
"items":[{"index":0,"verdict":"proved|contradicted|incomplete|too_weak|missing","note":"..."}]}

Rules:
- Pull concrete requirements from the objective. Do not shrink the original scope \
to whatever the audit already lists.
- For each audit item, the cited evidence must appear in the transcript (tool \
results, command output, files, pull-request state, tests). Evidence that is not \
in the transcript is missing.
- A green test counts only if it covers that requirement. Uncertain or indirect \
evidence is not proved.
- Intent, partial progress, memory of earlier turns, and a reasonable-sounding \
answer are not proof.
- verdict is pass only when every requirement of the objective is proved and no \
required work remains. Otherwise fail.
- uncovered_requirements lists requirements of the objective the audit did not prove.
- Do not invent facts that are not in the objective, audit, or transcript.
"""


@dataclass(frozen=True)
class AuditItem:
    requirement: str
    evidence: str
    verdict: str


@dataclass(frozen=True)
class CompletionVerdict:
    passed: bool
    reasons: tuple[str, ...] = ()
    notes: str = ""


class CompletionVerifier(Protocol):
    async def verify(
        self,
        objective: str,
        audit: Sequence[AuditItem],
        transcript: str,
        *,
        thread_id: str = "",
        request_id: str = "",
    ) -> CompletionVerdict:
        """Return whether the audit proves the objective. Fail closed."""


def parse_audit(raw: object) -> list[AuditItem] | str:
    """Parse an ``update_goal`` audit, or return an error string."""
    if not isinstance(raw, list) or not raw:
        return "update_goal complete requires a non-empty audit"
    items: list[AuditItem] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return f"audit[{index}] must be an object with requirement, evidence, and verdict"
        requirement = str(entry.get("requirement") or "").strip()
        evidence = str(entry.get("evidence") or "").strip()
        verdict = str(entry.get("verdict") or "").strip()
        if not requirement or not evidence:
            return f"audit[{index}] needs a non-empty requirement and evidence"
        if verdict not in AUDIT_VERDICTS:
            allowed = ", ".join(sorted(AUDIT_VERDICTS))
            return f"audit[{index}].verdict must be one of {allowed}"
        items.append(AuditItem(requirement=requirement, evidence=evidence, verdict=verdict))
    return items


def audit_blocks_completion(items: Sequence[AuditItem]) -> str | None:
    """Return an error when any requirement is not proved. None means the audit is eligible."""
    blocked = [item for item in items if item.verdict != "proved"]
    if not blocked:
        return None
    lines = [f"- {item.verdict}: {item.requirement}" for item in blocked]
    return (
        "completion is unproven; the goal stays active. "
        "Every requirement must be proved:\n" + "\n".join(lines)
    )


def transcript_excerpt(messages: Sequence[Message], *, limit: int = _TRANSCRIPT_MAX) -> str:
    """Tool calls and results from the end of the transcript, truncated."""
    chunks: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolRequest):
                chunks.append(f"tool {block.name} args={json.dumps(block.args, default=str)}")
            elif isinstance(block, ToolResponse):
                text = "".join(part.text for part in block.result if isinstance(part, Text))
                flag = " error" if block.is_error else ""
                chunks.append(f"result {block.tool_name}{flag}: {text}")
    body = "\n".join(chunks).strip()
    if len(body) <= limit:
        return body
    return body[-limit:]


def completion_evidence_json(
    audit: Sequence[AuditItem],
    verdict: CompletionVerdict,
) -> str:
    return json.dumps(
        {
            "audit": [
                {
                    "requirement": item.requirement,
                    "evidence": item.evidence,
                    "verdict": item.verdict,
                }
                for item in audit
            ],
            "judge": {
                "passed": verdict.passed,
                "reasons": list(verdict.reasons),
                "notes": verdict.notes,
            },
        }
    )


class AuditOnlyVerifier:
    """Accepts an audit that already passed the deterministic check."""

    async def verify(
        self,
        objective: str,
        audit: Sequence[AuditItem],
        transcript: str,
        *,
        thread_id: str = "",
        request_id: str = "",
    ) -> CompletionVerdict:
        del objective, audit, transcript, thread_id, request_id
        return CompletionVerdict(passed=True, notes="audit only")


class ScriptedCompletionVerifier:
    """Pops scripted outcomes: pass, fail, timeout, bad_json."""

    def __init__(self, results: Sequence[str]) -> None:
        self._results = list(results)
        self.calls = 0

    async def verify(
        self,
        objective: str,
        audit: Sequence[AuditItem],
        transcript: str,
        *,
        thread_id: str = "",
        request_id: str = "",
    ) -> CompletionVerdict:
        del objective, audit, transcript, thread_id, request_id
        self.calls += 1
        mode = self._results.pop(0) if self._results else "pass"
        if mode == "timeout":
            return CompletionVerdict(passed=False, reasons=("completion judge timed out",))
        if mode == "bad_json":
            return CompletionVerdict(
                passed=False,
                reasons=("completion judge returned unusable output",),
            )
        if mode == "fail":
            return CompletionVerdict(
                passed=False,
                reasons=("requirement not covered by the transcript",),
            )
        return CompletionVerdict(passed=True, notes="scripted pass")


class ProviderCompletionJudge:
    """One blocking provider call. Fails closed when it cannot return a pass."""

    def __init__(
        self,
        provider: ProviderRef,
        *,
        model: ModelRef,
        timeout_s: float = 30.0,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_s = timeout_s

    async def verify(
        self,
        objective: str,
        audit: Sequence[AuditItem],
        transcript: str,
        *,
        thread_id: str = "",
        request_id: str = "",
    ) -> CompletionVerdict:
        provider = resolve_live_provider(self._provider)
        model = resolve_live_model(self._model, "")
        if provider is None or not model:
            logger.warning(
                "completion judge unavailable %s",
                kv(has_provider=provider is not None, model=model or ""),
            )
            return CompletionVerdict(
                passed=False,
                reasons=("completion judge unavailable",),
            )
        messages = [
            Message(role="system", content=[Text(text=_JUDGE_SYSTEM)]),
            Message(
                role="user",
                content=[Text(text=_judge_user_blob(objective, audit, transcript))],
            ),
        ]
        try:
            text, _tokens = await asyncio.wait_for(
                collect_verifier_stream(
                    provider,
                    messages,
                    model,
                    thread_id=thread_id,
                    request_id=request_id,
                    log_event="completion judge stream",
                    log=logger,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "completion judge timed out %s",
                kv(thread_id=thread_id, request_id=request_id, timeout_s=self._timeout_s),
            )
            return CompletionVerdict(passed=False, reasons=("completion judge timed out",))
        parsed = _parse_completion_judge(text)
        if parsed is None:
            logger.warning(
                "completion judge unparseable %s",
                kv(thread_id=thread_id, request_id=request_id, chars=len(text)),
            )
            return CompletionVerdict(
                passed=False,
                reasons=("completion judge returned unusable output",),
            )
        return parsed


def _judge_user_blob(objective: str, audit: Sequence[AuditItem], transcript: str) -> str:
    lines = [
        f"{index}. requirement: {item.requirement}\n"
        f"   verdict: {item.verdict}\n"
        f"   evidence: {item.evidence}"
        for index, item in enumerate(audit)
    ]
    audit_text = "\n".join(lines) if lines else "(empty)"
    seen = transcript.strip() or "(no tool results in the transcript)"
    return (
        f"Objective:\n{objective.strip()}\n\n"
        f"Audit:\n{audit_text}\n\n"
        f"Transcript tool results:\n{seen}"
    )


def _parse_completion_judge(raw: str) -> CompletionVerdict | None:
    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail"}:
        return None
    reasons: list[str] = []
    uncovered = payload.get("uncovered_requirements")
    if isinstance(uncovered, list):
        reasons.extend(
            f"uncovered: {item.strip()}"
            for item in uncovered
            if isinstance(item, str) and item.strip()
        )
    notes: list[str] = []
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_verdict = str(item.get("verdict") or "").strip()
            note = str(item.get("note") or "").strip()
            if item_verdict and item_verdict != "proved":
                label = note or item_verdict
                reasons.append(f"{item_verdict}: {label}"[:_NOTE_MAX])
            elif note:
                notes.append(note[:_NOTE_MAX])
    if verdict == "fail" and not reasons:
        reasons.append("completion judge rejected the audit")
    return CompletionVerdict(
        passed=verdict == "pass",
        reasons=tuple(reasons),
        notes="; ".join(notes)[:_NOTE_MAX],
    )
