"""Classifier port and JSON parser for incremental goal-ledger updates."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from contextlib import aclosing
from typing import Any, Protocol, cast

from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.goal_ledger import (
    Classification,
    ConstraintDraft,
    ConstraintKind,
    GoalEntry,
    Intent,
)
from monkeybot.core.types.content_blocks import Text

logger = logging.getLogger(__name__)

_CLASSIFIER_SYSTEM = """\
You classify one new human message against the currently open goals.
Return ONLY compact JSON with this schema:
{"intent":"new_goal|refinement|scope_change|correction|preempt|answer|noise",\
"relates_to":"<entry_id or null>",\
"constraints":[{"kind":"path_glob|tool_name|command_regex|free_text","pattern":"...","verbatim":"..."}],\
"done_when":["..."]}

Rules:
- intent is required. Use new_goal when this starts work, refinement when it narrows the open goal,
  scope_change when it replaces the open goal, correction when the user forbids or undoes something,
  preempt when they want something else first (do not abandon the open goal),
  answer when they are answering the agent, noise otherwise.
- relates_to must be an open entry_id when intent is refinement, scope_change, correction, or preempt.
- Prefer typed constraints: path_glob for file/dir globs, tool_name for a tool, command_regex for shell.
  Use free_text only when you cannot type it. pattern is the glob/name/regex; verbatim is the user's words.
- done_when is optional success criteria, short.
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
    ) -> Classification: ...


class ScriptedClassifier:
    """Test double: returns queued classifications, optionally delaying."""

    def __init__(
        self,
        results: Sequence[Classification],
        *,
        delay_s: float = 0.0,
    ) -> None:
        self._results = list(results)
        self._delay_s = delay_s
        self.calls: list[str] = []

    async def classify(
        self,
        verbatim: str,
        open_entries: Sequence[GoalEntry],
    ) -> Classification:
        del open_entries
        import asyncio

        self.calls.append(verbatim)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        if not self._results:
            return Classification(intent=Intent.NOISE, relates_to=None, constraints=(), done_when=())
        return self._results.pop(0)


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
    done_raw = payload.get("done_when") or []
    done_when = tuple(str(x).strip() for x in done_raw if str(x).strip()) if isinstance(done_raw, list) else ()
    return Classification(
        intent=intent,
        relates_to=relates_to,
        constraints=tuple(drafts),
        done_when=done_when,
    )


def fail_open_classification(open_entries: Sequence[GoalEntry]) -> Classification:
    """Persist the verbatim without inventing constraints if the model fails."""
    if any(e.status.value == "active" for e in open_entries):
        return Classification(intent=Intent.ANSWER, relates_to=None, constraints=(), done_when=())
    return Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=(), done_when=())


class ProviderClassifier:
    """One small provider call per human input. Never raises to the caller."""

    def __init__(
        self,
        provider: Provider | Callable[[], Provider | None] | None,
        *,
        model: str,
    ) -> None:
        self._provider = provider
        self._model = model

    def bind_provider(self, provider: Provider | Callable[[], Provider | None] | None) -> None:
        self._provider = provider

    def _current_provider(self) -> Provider | None:
        provider = self._provider
        if callable(provider):
            return provider()
        return provider

    async def classify(
        self,
        verbatim: str,
        open_entries: Sequence[GoalEntry],
    ) -> Classification:
        provider = self._current_provider()
        if provider is None:
            return fail_open_classification(open_entries)
        open_blob = _open_entries_blob(open_entries)
        messages = [
            Message(role="system", content=[Text(text=_CLASSIFIER_SYSTEM)]),
            Message(
                role="user",
                content=[
                    Text(
                        text=(
                            f"Open entries:\n{open_blob}\n\nNew human message:\n{verbatim}"
                        )
                    )
                ],
            ),
        ]
        text = ""
        try:
            async with aclosing(cast(Any, provider.stream(messages, [], model=self._model))) as stream:
                async for ev in stream:
                    if isinstance(ev, TextDelta):
                        text += ev.text
                    elif isinstance(ev, Done):
                        break
        except Exception:
            logger.warning(
                "goal_ledger classifier failed %s",
                kv(model=self._model),
                exc_info=True,
            )
            return fail_open_classification(open_entries)
        parsed = parse_classification(text)
        if parsed is None:
            logger.warning(
                "goal_ledger classifier unparseable %s",
                kv(model=self._model, chars=len(text)),
            )
            return fail_open_classification(open_entries)
        return parsed


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
