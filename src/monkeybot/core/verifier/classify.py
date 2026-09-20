"""Classifier port and JSON parser for incremental goal-ledger updates."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Sequence
from contextlib import aclosing
from typing import Any, Protocol, cast

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta, UsageEvent
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.goal_ledger import (
    Classification,
    ConstraintDraft,
    ConstraintKind,
    GoalEntry,
    Intent,
)
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.verifier.binding import (
    ModelRef,
    ProviderRef,
    resolve_live_model,
    resolve_live_provider,
)
from monkeybot.observability.spans import set_llm_io, set_llm_usage, span_llm

logger = logging.getLogger(__name__)

_CLASSIFIER_TIMEOUT_S = 15.0

_CLASSIFIER_SYSTEM = """\
You classify one new human message against the currently open goals.
Return ONLY compact JSON with this schema:
{"intent":"new_goal|refinement|scope_change|correction|preempt|answer|noise",\
"relates_to":"<entry_id or null>",\
"constraints":[{"kind":"path_glob|tool_name|command_regex|free_text","pattern":"...","verbatim":"..."}]}

Rules:
- intent is required. Use new_goal when this starts work, refinement when it narrows the open goal,
  scope_change when it replaces the open goal, correction when the user forbids or undoes something,
  preempt when they want something else first (do not abandon the open goal),
  answer when they are answering the agent, noise otherwise.
- relates_to must be an open entry_id when intent is refinement, scope_change, correction, or preempt.
- Prefer typed constraints: path_glob for file/dir globs, tool_name for a tool, command_regex for shell.
  Use free_text only when you cannot type it. pattern is the glob/name/regex; verbatim is the user's words.
- Do not invent constraints the user did not state.
"""

_INTENT_VALUES = {item.value: item for item in Intent}
_KIND_VALUES = {item.value: item for item in ConstraintKind}
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ClassifierPort(Protocol):
    async def classify(
        self,
        verbatim: str,
        open_entries: Sequence[GoalEntry],
        *,
        thread_id: str = "",
    ) -> Classification: ...


def parse_classification(raw: str) -> Classification | None:
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
    intent_raw = str(payload.get("intent") or "").strip()
    intent = _INTENT_VALUES.get(intent_raw)
    if intent is None:
        return None
    relates = payload.get("relates_to")
    relates_to = str(relates).strip() if relates else None
    if relates_to in ("", "null", "None"):
        relates_to = None
    drafts: list[ConstraintDraft] = []
    raw_constraints = payload.get("constraints") or []
    if isinstance(raw_constraints, list):
        for item in raw_constraints:
            if not isinstance(item, dict):
                continue
            kind = _KIND_VALUES.get(str(item.get("kind") or "").strip(), ConstraintKind.FREE_TEXT)
            pattern = str(item.get("pattern") or "").strip()
            verbatim = str(item.get("verbatim") or "").strip()
            if not pattern and not verbatim:
                continue
            drafts.append(
                ConstraintDraft(
                    kind=kind,
                    pattern=pattern or verbatim,
                    verbatim=verbatim or pattern,
                )
            )
    return Classification(
        intent=intent,
        relates_to=relates_to,
        constraints=tuple(drafts),
    )


def fail_open_classification(open_entries: Sequence[GoalEntry]) -> Classification:
    """Persist the verbatim without inventing constraints if the model fails.

    Callers pass currently-active human entries. Any non-empty list means an
    open goal exists, so we record the utterance as an answer rather than a
    new goal.
    """
    if open_entries:
        return Classification(intent=Intent.ANSWER, relates_to=None, constraints=())
    return Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=())


class ProviderClassifier:
    """One small provider call per human input. Never raises to the caller."""

    def __init__(
        self,
        provider: ProviderRef,
        *,
        model: ModelRef,
        timeout_s: float = _CLASSIFIER_TIMEOUT_S,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_s = timeout_s

    async def classify(
        self,
        verbatim: str,
        open_entries: Sequence[GoalEntry],
        *,
        thread_id: str = "",
    ) -> Classification:
        provider = resolve_live_provider(self._provider)
        model = resolve_live_model(self._model)
        if provider is None or not model:
            logger.warning(
                "classifier skipped: no provider or model %s",
                kv(has_provider=provider is not None, model=model or "", thread_id=thread_id),
            )
            return fail_open_classification(open_entries)
        open_blob = _open_entries_blob(open_entries)
        messages = [
            Message(role="system", content=[Text(text=_CLASSIFIER_SYSTEM)]),
            Message(
                role="user",
                content=[
                    Text(text=(f"Open entries:\n{open_blob}\n\nNew human message:\n{verbatim}"))
                ],
            ),
        ]
        try:
            text, _tokens = await asyncio.wait_for(
                collect_verifier_stream(
                    provider,
                    messages,
                    model,
                    thread_id=thread_id,
                    request_id=thread_id,
                    log_event="goal_ledger classifier stream",
                    log=logger,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "goal_ledger classifier timed out %s",
                kv(model=model, timeout_s=self._timeout_s, thread_id=thread_id),
            )
            return fail_open_classification(open_entries)
        except Exception:
            logger.warning(
                "goal_ledger classifier failed %s",
                kv(model=model, thread_id=thread_id),
                exc_info=True,
            )
            return fail_open_classification(open_entries)
        parsed = parse_classification(text)
        if parsed is None:
            logger.warning(
                "goal_ledger classifier unparseable %s",
                kv(model=model, chars=len(text), thread_id=thread_id),
            )
            return fail_open_classification(open_entries)
        return parsed


async def collect_verifier_stream(
    provider: Provider,
    messages: list[Message],
    model: str,
    *,
    thread_id: str,
    request_id: str,
    log_event: str,
    log: logging.Logger,
) -> tuple[str, int]:
    prompt = "\n".join(
        "".join(b.text for b in message.content if isinstance(b, Text)) for message in messages
    )
    ctx = TurnContext(
        thread_id=thread_id,
        request_id=request_id,
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model=model,
    )
    started = time.monotonic()
    text = ""
    input_tokens = 0
    output_tokens = 0
    async with span_llm(ctx=ctx, model=model):
        async with aclosing(cast(Any, provider.stream(messages, [], model=model))) as stream:
            async for ev in stream:
                if isinstance(ev, TextDelta):
                    text += ev.text
                elif isinstance(ev, UsageEvent):
                    input_tokens += max(0, ev.input_tokens)
                    output_tokens += max(0, ev.output_tokens)
                elif isinstance(ev, Done):
                    break
        set_llm_io(prompt=prompt, completion=text)
        if input_tokens or output_tokens:
            set_llm_usage(input_tokens=input_tokens, output_tokens=output_tokens)
    tokens = input_tokens + output_tokens
    log.info(
        "%s %s",
        log_event,
        kv(
            thread_id=thread_id,
            request_id=request_id,
            model=model,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            chars=len(text),
        ),
    )
    return text, tokens


def _open_entries_blob(open_entries: Sequence[GoalEntry]) -> str:
    if not open_entries:
        return "(none)"
    lines: list[str] = []
    for entry in open_entries:
        lines.append(
            f"- id={entry.entry_id} status={entry.status.value} intent={entry.intent.value} "
            f"text={entry.verbatim[:240]}"
        )
    return "\n".join(lines)
