"""Path tokens, script compilation, and the scripted providers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.testing.mocks_provider import RaiseStep, _exception_class
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from regression.drivers.bundles.protocol import LoopDriverError
from regression.drivers.support.paths import (
    _GRANT_ROOT_TOKEN,
    _JUDGE_SYSTEM_PREFIX,
    _REPO,
    _REPO_ROOT_TOKEN,
    _RESULT_FIELD_TOKENS,
    _WORKSPACE_TOKEN,
)
from regression.schema.events import RaiseEvent, to_provider_event
from regression.schema.scenario import Scenario
from testkit.provider import ScriptedProvider
from testkit.verifier import ScriptedJudge


def _expand_tokens(text: str, tokens: Mapping[str, str]) -> str:
    """Replace scenario path tokens. Unknown tokens are left as written."""
    for token, value in tokens.items():
        if token in text:
            text = text.replace(token, value)
    return text


def _expand_tokens_data(value: Any, tokens: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _expand_tokens(value, tokens)
    if isinstance(value, dict):
        return {key: _expand_tokens_data(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_tokens_data(item, tokens) for item in value]
    return value


def _repo_tokens() -> dict[str, str]:
    return {_REPO_ROOT_TOKEN: _REPO.as_posix()}


def _expand_repo_root(text: str) -> str:
    """Replace ``{repo_root}`` with this checkout.

    T2 MCP configs name ``evals/smoke_agent/fixture_mcp_server.py`` once, and
    scripted tool arguments can use the same token when they rewrite ``mcp.json``.
    """
    return _expand_tokens(text, _repo_tokens())


def _scenario_tokens(workspace: Path | None, policy: Any) -> dict[str, str]:
    tokens = _repo_tokens()
    if workspace is not None:
        tokens[_WORKSPACE_TOKEN] = workspace.resolve().as_posix()
    grant_root = None if policy is None else getattr(policy, "grant_root", None)
    if grant_root is not None:
        tokens[_GRANT_ROOT_TOKEN] = grant_root.resolve().as_posix()
    return tokens


def _user_messages(
    scenario: Scenario,
    workspace: Path | None,
    tokens: Mapping[str, str] | None = None,
) -> list[str]:
    active = tokens if tokens is not None else _repo_tokens()
    messages = [_expand_tokens(message, active) for message in scenario.messages]
    if workspace is None:
        return messages
    name = workspace.name
    return [message.replace("{workspace_subdir}", name) for message in messages]


def _session_groups(
    scenario: Scenario,
    messages: list[str],
    workspace: Path | None,
    tokens: Mapping[str, str],
) -> list[list[str]]:
    """One group per session. A ``messages`` scenario is a single group."""
    if scenario.sessions is None:
        return [messages]
    groups: list[list[str]] = []
    for group in scenario.sessions:
        expanded = [_expand_tokens(message, tokens) for message in group]
        if workspace is not None:
            name = workspace.name
            expanded = [message.replace("{workspace_subdir}", name) for message in expanded]
        groups.append(expanded)
    return groups


def _render_result_field(raw: object) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return None


def _prior_result_fields(messages: Sequence[Any]) -> dict[str, str]:
    """Latest ``job_id`` and ``cursor`` from tool-result JSON in ``messages``.

    Those messages are the conversation the provider is about to answer. The
    values are not read from the executor's job table.
    """
    keys = {key for _, key in _RESULT_FIELD_TOKENS}
    found: dict[str, str] = {}
    for message in messages:
        content = getattr(message, "content", ())
        for block in content:
            if not isinstance(block, ToolResponse):
                continue
            for inner in block.result:
                if not isinstance(inner, Text) or not inner.text:
                    continue
                try:
                    parsed = json.loads(inner.text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                for key in keys:
                    rendered = _render_result_field(parsed.get(key))
                    if rendered is not None:
                        found[key] = rendered
    return found


def _contains_result_token(value: Any) -> bool:
    if isinstance(value, str):
        return any(token in value for token, _key in _RESULT_FIELD_TOKENS)
    if isinstance(value, dict):
        return any(_contains_result_token(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_result_token(item) for item in value)
    return False


def _expand_result_tokens(value: Any, fields: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for token, key in _RESULT_FIELD_TOKENS:
            if token not in value:
                continue
            replacement = fields.get(key)
            if replacement is None:
                raise LoopDriverError(
                    f"script token {token} has no prior tool result field {key!r}"
                )
            value = value.replace(token, replacement)
        return value
    if isinstance(value, dict):
        return {key: _expand_result_tokens(item, fields) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_result_tokens(item, fields) for item in value]
    return value


def _bind_prior_tool_fields(event: ToolCall, messages: Sequence[Any]) -> ToolCall:
    if not _contains_result_token(event.args):
        return event
    expanded = _expand_result_tokens(dict(event.args), _prior_result_fields(messages))
    if expanded == event.args:
        return event
    return replace(event, args=expanded)


class _ResultTokenProvider(ScriptedProvider):
    """Scripted provider that fills ``{job_id}`` and ``{cursor}`` at stream time."""

    def __init__(self, turns: Sequence[Sequence[Any]], *, name: str = "fake") -> None:
        super().__init__(turns, name=name)
        self.stream_thinking_budgets: list[int | None] = []
        self.stream_vertex_google_search: list[bool] = []

    async def stream(
        self,
        messages: Sequence[Any],
        tools: Sequence[Any],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: Any = None,
        vertex_google_search: bool = False,
    ) -> Any:
        self.stream_thinking_budgets.append(thinking_budget)
        self.stream_vertex_google_search.append(bool(vertex_google_search))
        async for event in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            hints=hints,
        ):
            if isinstance(event, ToolCall):
                event = _bind_prior_tool_fields(event, messages)
            yield event


class _JudgeScriptProvider(_ResultTokenProvider):
    """Agent script, except judge prompts, which read ``ScriptedJudge``.

    ``ProviderJudge`` calls the provider bound for the session. That object is
    the agent. Judge completions stay on ``ScriptedJudge`` so they do not
    consume the agent script.
    """

    def __init__(
        self,
        turns: Sequence[Sequence[Any]],
        judge: ScriptedJudge,
        *,
        name: str = "fake",
    ) -> None:
        super().__init__(turns, name=name)
        self._judge = judge

    async def stream(
        self,
        messages: Sequence[Any],
        tools: Sequence[Any],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: Any = None,
        vertex_google_search: bool = False,
    ) -> Any:
        if _is_judge_request(messages):
            async for event in self._judge.stream(
                messages,
                tools,
                model=model,
                thinking_budget=thinking_budget,
            ):
                yield event
            return
        async for event in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            hints=hints,
            vertex_google_search=vertex_google_search,
        ):
            yield event


def _message_text(message: object) -> str:
    """Text the provider-request matcher can see, including tool blocks.

    Adjacent ``Text`` blocks stay concatenated. Tool requests add their id,
    name, and metadata. Tool responses add their id, name, and result text.
    """
    content = getattr(message, "content", ())
    parts: list[str] = []
    for block in content:
        if isinstance(block, Text):
            parts.append(block.text)
        else:
            rendered = _tool_block_text(block)
            if rendered:
                parts.append(rendered)
    return "".join(parts)


def _tool_block_text(block: object) -> str:
    if isinstance(block, ToolRequest):
        meta = block.metadata or {}
        extra = " ".join(f"{key}={value}" for key, value in meta.items() if value is not None)
        base = f"toolRequest {block.id} {block.name}"
        return f"{base} {extra}".rstrip()
    if isinstance(block, ToolResponse):
        inner = "".join(
            child.text if isinstance(child, Text) else _tool_block_text(child)
            for child in block.result
        )
        base = f"toolResponse {block.id} {block.tool_name}"
        return f"{base} {inner}".rstrip()
    return ""


def _is_judge_request(messages: Sequence[Any]) -> bool:
    if not messages:
        return False
    first = messages[0]
    if getattr(first, "role", None) != "system":
        return False
    return _message_text(first).lstrip().startswith(_JUDGE_SYSTEM_PREFIX)


def _script_turns(
    scenario: Scenario,
    tokens: Mapping[str, str] | None = None,
) -> list[list[Any]]:
    active = tokens if tokens is not None else _repo_tokens()
    if scenario.script is None:
        raise LoopDriverError("scripted loop scenario is missing script")
    turns: list[list[Any]] = []
    for index, turn in enumerate(scenario.script):
        compiled: list[Any] = []
        where = f"{scenario.case} script turn {index + 1}"
        for event in turn:
            if isinstance(event, RaiseEvent):
                compiled.append(
                    RaiseStep(
                        error=_exception_class(event.error, where=where),
                        message=event.message or "",
                        after=event.after,
                    )
                )
            else:
                provider_event = to_provider_event(event)
                if isinstance(provider_event, ToolCall):
                    expanded = _expand_tokens_data(dict(provider_event.args), active)
                    if expanded != provider_event.args:
                        provider_event = replace(provider_event, args=expanded)
                compiled.append(provider_event)
        turns.append(compiled)
    return turns
