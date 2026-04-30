"""Patch deepagents task tool: forward RunnableConfig to subagents + optional retries."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar, Token
from typing import Annotated, Any

import deepagents.middleware.subagents as _deepagents_subagents
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.runnables.config import RunnableConfig, ensure_config, merge_configs
from langchain_core.tools import StructuredTool
from langgraph.types import Command

_applied = False
_logger = logging.getLogger(__name__)

MAX_SUBAGENT_RETRIES = 5

# Populated when the patched _build_task_tool runs during agent init (retry-wrapped runnables).
SUBAGENT_RUNNABLES: dict[str, Runnable] = {}

# Per ``build_deep_agent`` / ``create_deep_agent`` call — ``apply_task_tool_patches`` is idempotent.
_SUBAGENT_INVOCATION_CTX: ContextVar[Callable[[str], AbstractContextManager[Any]] | None] = ContextVar(
    "harness_subagent_invocation_ctx_factory",
    default=None,
)


@contextmanager
def subagent_invocation_ctx_scope(
    ctx: Callable[[str], AbstractContextManager[Any]] | None,
) -> Any:
    tok: Token[Callable[[str], AbstractContextManager[Any]] | None] = _SUBAGENT_INVOCATION_CTX.set(ctx)
    try:
        yield
    finally:
        _SUBAGENT_INVOCATION_CTX.reset(tok)


def _current_subagent_invocation_ctx() -> Callable[[str], AbstractContextManager[Any]] | None:
    return _SUBAGENT_INVOCATION_CTX.get()


def _invoke_config_for_subagent(runtime: ToolRuntime) -> RunnableConfig:
    """Merge parent RunnableConfig (callbacks, tags, … from context) with tool runtime.config.

    Passing only ``runtime.config`` drops LangGraph-inherited callbacks, so nested subagent LLM
    calls never reach handlers such as token-usage accumulators on the HTTP run.
    """
    rc = getattr(runtime, "config", None)
    return merge_configs(ensure_config(), rc if rc is not None else None)


class _RetrySubagentRunnable(Runnable):
    """Wraps a subagent runnable with retry logic."""

    def __init__(self, wrapped: Runnable, name: str, max_retries: int = MAX_SUBAGENT_RETRIES) -> None:
        self._wrapped = wrapped
        self._name = name
        self._max_retries = max_retries

    def invoke(self, input, config=None, **kwargs):
        last_error: BaseException | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._wrapped.invoke(input, config=config, **kwargs)
            except Exception as exc:
                last_error = exc
                try:
                    from .harness.run_package_accumulator import record_subagent_attempt

                    record_subagent_attempt(attempt, str(exc), "unexpected")
                except Exception:  # noqa: BLE001
                    pass
                _logger.warning(
                    "subagent_retry",
                    extra={
                        "subagent": self._name,
                        "attempt": attempt,
                        "max_retries": self._max_retries,
                        "error": str(exc),
                    },
                )
        return {
            "messages": [
                HumanMessage(
                    content=f"Subagent '{self._name}' failed after {self._max_retries} retries. "
                    f"Last error: {last_error}. Please handle this gracefully and report the failure."
                )
            ]
        }

    async def ainvoke(self, input, config=None, **kwargs):
        last_error: BaseException | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._wrapped.ainvoke(input, config=config, **kwargs)
            except Exception as exc:
                last_error = exc
                try:
                    from .harness.run_package_accumulator import record_subagent_attempt

                    record_subagent_attempt(attempt, str(exc), "unexpected")
                except Exception:  # noqa: BLE001
                    pass
                _logger.warning(
                    "subagent_retry",
                    extra={
                        "subagent": self._name,
                        "attempt": attempt,
                        "max_retries": self._max_retries,
                        "error": str(exc),
                    },
                )
        return {
            "messages": [
                HumanMessage(
                    content=f"Subagent '{self._name}' failed after {self._max_retries} retries. "
                    f"Last error: {last_error}. Please handle this gracefully and report the failure."
                )
            ]
        }


def _build_task_tool_forwarding_config(  # type: ignore[no-untyped-def]
    subagents,
    task_description=None,
    *,
    subagent_invocation_ctx: Callable[[str], AbstractContextManager[Any]] | None = None,
):
    """Mirror of deepagents ``_build_task_tool`` but passes ``config=runtime.config`` into subagents.

    Keeps ``RunnableConfig`` (e.g. ``configurable['memory_context_dir']``) on ``task`` delegation.
    Sync with ``deepagents.middleware.subagents._build_task_tool`` when upgrading deepagents.
    """
    ctx_factory = subagent_invocation_ctx or _current_subagent_invocation_ctx() or (lambda _name: nullcontext())

    subagent_graphs: dict[str, Runnable] = {spec["name"]: spec["runnable"] for spec in subagents}
    subagent_description_str = "\n".join(f"- {s['name']}: {s['description']}" for s in subagents)

    if task_description is None:
        description = _deepagents_subagents.TASK_TOOL_DESCRIPTION.format(available_agents=subagent_description_str)
    elif "{available_agents}" in task_description:
        description = task_description.format(available_agents=subagent_description_str)
    else:
        description = task_description

    excluded = _deepagents_subagents._EXCLUDED_STATE_KEYS

    def _return_command_with_state_update(result: dict, tool_call_id: str) -> Command:
        if "messages" not in result:
            error_msg = (
                "CompiledSubAgent must return a state containing a 'messages' key. "
                "Custom StateGraphs used with CompiledSubAgent should include 'messages' "
                "in their state schema to communicate results back to the main agent."
            )
            raise ValueError(error_msg)

        state_update = {k: v for k, v in result.items() if k not in excluded}
        message_text = result["messages"][-1].text.rstrip() if result["messages"][-1].text else ""
        return Command(
            update={
                **state_update,
                "messages": [ToolMessage(message_text, tool_call_id=tool_call_id)],
            }
        )

    def _validate_and_prepare_state(subagent_type: str, description: str, runtime: ToolRuntime) -> tuple[Runnable, dict]:
        subagent = subagent_graphs[subagent_type]
        subagent_state = {k: v for k, v in runtime.state.items() if k not in excluded}
        subagent_state["messages"] = [HumanMessage(content=description)]
        return subagent, subagent_state

    def task(
        description: Annotated[
            str,
            "A detailed description of the task for the subagent to perform autonomously. Include all necessary context and specify the expected output format.",  # noqa: E501
        ],
        subagent_type: Annotated[str, "The type of subagent to use. Must be one of the available agent types listed in the tool description."],
        runtime: ToolRuntime,
    ) -> str | Command:
        from .harness.errors import RecursionBudgetExceeded
        from .harness.middleware.principal_propagation import (
            current_principal,
            current_run_id,
            current_session_id,
        )
        from .harness.run_package_accumulator import (
            ACTIVE_SUBAGENT_HOOKS,
            event_parent_run_id_for_spawn,
            subagent_attempts_reset,
            subagent_attempts_token,
        )

        if subagent_type not in subagent_graphs:
            allowed_types = ", ".join([f"`{k}`" for k in subagent_graphs])
            msg = (
                f"We cannot invoke subagent {subagent_type} because it does not exist, "
                f"the only allowed types are {allowed_types}"
            )
            hooks = ACTIVE_SUBAGENT_HOOKS.get()
            if hooks is not None:
                hooks.record_invalid_subagent_delegation(
                    subagent_type=subagent_type,
                    description=description,
                    message=msg,
                    session_id=current_session_id(),
                    principal=current_principal(),
                    parent_run_id=event_parent_run_id_for_spawn(hooks, current_run_id()),
                    parent_tool_call_id=runtime.tool_call_id,
                )
            return msg
        subagent, subagent_state = _validate_and_prepare_state(subagent_type, description, runtime)
        hooks = ACTIVE_SUBAGENT_HOOKS.get()
        attempts_tok = subagent_attempts_token()
        try:
            with ctx_factory(subagent_type):
                if hooks is not None:
                    hooks.start_subagent(
                        subagent_type=subagent_type,
                        description=description,
                        session_id=current_session_id(),
                        principal=current_principal(),
                        parent_run_id=event_parent_run_id_for_spawn(hooks, current_run_id()),
                        parent_tool_call_id=runtime.tool_call_id,
                    )
                    try:
                        result = subagent.invoke(subagent_state, config=_invoke_config_for_subagent(runtime))
                    except RecursionBudgetExceeded:
                        attempts = subagent_attempts_reset(attempts_tok)
                        attempts_tok = None
                        hooks.finish_failure(
                            messages=[
                                HumanMessage(
                                    content="Subagent recursion budget exceeded.",
                                )
                            ],
                            attempts=attempts or None,
                        )
                        raise
                    except Exception as exc:  # noqa: BLE001
                        attempts = subagent_attempts_reset(attempts_tok)
                        attempts_tok = None
                        hooks.finish_failure(
                            messages=[HumanMessage(content=f"Subagent error: {type(exc).__name__}: {exc}")],
                            attempts=attempts or None,
                        )
                        raise
                    attempts = subagent_attempts_reset(attempts_tok)
                    attempts_tok = None
                    outcome = _subagent_outcome_from_result_and_attempts(result, attempts)
                    if outcome == "pass":
                        hooks.finish_success(result, attempts or None)
                    else:
                        hooks.finish_failure(
                            messages=list(result.get("messages") or []),
                            outcome=outcome,
                            attempts=attempts or None,
                        )
                else:
                    result = subagent.invoke(subagent_state, config=_invoke_config_for_subagent(runtime))
        finally:
            if attempts_tok is not None:
                subagent_attempts_reset(attempts_tok)
        if not runtime.tool_call_id:
            value_error_msg = "Tool call ID is required for subagent invocation"
            raise ValueError(value_error_msg)
        return _return_command_with_state_update(result, runtime.tool_call_id)

    async def atask(
        description: Annotated[
            str,
            "A detailed description of the task for the subagent to perform autonomously. Include all necessary context and specify the expected output format.",  # noqa: E501
        ],
        subagent_type: Annotated[str, "The type of subagent to use. Must be one of the available agent types listed in the tool description."],
        runtime: ToolRuntime,
    ) -> str | Command:
        from .harness.errors import RecursionBudgetExceeded
        from .harness.middleware.principal_propagation import (
            current_principal,
            current_run_id,
            current_session_id,
        )
        from .harness.run_package_accumulator import (
            ACTIVE_SUBAGENT_HOOKS,
            event_parent_run_id_for_spawn,
            subagent_attempts_reset,
            subagent_attempts_token,
        )

        if subagent_type not in subagent_graphs:
            allowed_types = ", ".join([f"`{k}`" for k in subagent_graphs])
            msg = (
                f"We cannot invoke subagent {subagent_type} because it does not exist, "
                f"the only allowed types are {allowed_types}"
            )
            hooks = ACTIVE_SUBAGENT_HOOKS.get()
            if hooks is not None:
                hooks.record_invalid_subagent_delegation(
                    subagent_type=subagent_type,
                    description=description,
                    message=msg,
                    session_id=current_session_id(),
                    principal=current_principal(),
                    parent_run_id=event_parent_run_id_for_spawn(hooks, current_run_id()),
                    parent_tool_call_id=runtime.tool_call_id,
                )
            return msg
        subagent, subagent_state = _validate_and_prepare_state(subagent_type, description, runtime)
        hooks = ACTIVE_SUBAGENT_HOOKS.get()
        attempts_tok = subagent_attempts_token()
        try:
            with ctx_factory(subagent_type):
                if hooks is not None:
                    hooks.start_subagent(
                        subagent_type=subagent_type,
                        description=description,
                        session_id=current_session_id(),
                        principal=current_principal(),
                        parent_run_id=event_parent_run_id_for_spawn(hooks, current_run_id()),
                        parent_tool_call_id=runtime.tool_call_id,
                    )
                    try:
                        result = await subagent.ainvoke(subagent_state, config=_invoke_config_for_subagent(runtime))
                    except RecursionBudgetExceeded:
                        attempts = subagent_attempts_reset(attempts_tok)
                        attempts_tok = None
                        hooks.finish_failure(
                            messages=[
                                HumanMessage(
                                    content="Subagent recursion budget exceeded.",
                                )
                            ],
                            attempts=attempts or None,
                        )
                        raise
                    except Exception as exc:  # noqa: BLE001
                        attempts = subagent_attempts_reset(attempts_tok)
                        attempts_tok = None
                        hooks.finish_failure(
                            messages=[HumanMessage(content=f"Subagent error: {type(exc).__name__}: {exc}")],
                            attempts=attempts or None,
                        )
                        raise
                    attempts = subagent_attempts_reset(attempts_tok)
                    attempts_tok = None
                    outcome = _subagent_outcome_from_result_and_attempts(result, attempts)
                    if outcome == "pass":
                        hooks.finish_success(result, attempts or None)
                    else:
                        hooks.finish_failure(
                            messages=list(result.get("messages") or []),
                            outcome=outcome,
                            attempts=attempts or None,
                        )
                else:
                    result = await subagent.ainvoke(subagent_state, config=_invoke_config_for_subagent(runtime))
        finally:
            if attempts_tok is not None:
                subagent_attempts_reset(attempts_tok)
        if not runtime.tool_call_id:
            value_error_msg = "Tool call ID is required for subagent invocation"
            raise ValueError(value_error_msg)
        return _return_command_with_state_update(result, runtime.tool_call_id)

    return StructuredTool.from_function(
        name="task",
        func=task,
        coroutine=atask,
        description=description,
    )


def _subagent_outcome_from_result_and_attempts(
    result: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> str:
    """``pass``, ``fail``, or ``pass-with-warnings`` based on retry attempts + terminal message."""
    messages = list(result.get("messages") or [])
    if not attempts:
        return "pass"
    last = messages[-1] if messages else None
    text = getattr(last, "content", "") or "" if last is not None else ""
    if "failed after" in text and "retries" in text:
        return "fail"
    return "pass-with-warnings"


def _build_task_tool_with_retry(subagents, task_description=None, **kwargs):  # type: ignore[no-untyped-def]
    wrapped = [
        {**spec, "runnable": _RetrySubagentRunnable(spec["runnable"], spec["name"])}
        for spec in subagents
    ]
    SUBAGENT_RUNNABLES.clear()
    SUBAGENT_RUNNABLES.update({spec["name"]: spec["runnable"] for spec in wrapped})
    return _build_task_tool_forwarding_config(wrapped, task_description, **kwargs)


def apply_task_tool_patches(
    *,
    subagent_invocation_ctx: Callable[[str], AbstractContextManager[Any]] | None = None,
    max_retries: int = MAX_SUBAGENT_RETRIES,
) -> None:
    """Idempotent: patches ``_build_task_tool`` once per process."""

    global _applied
    if _applied:
        return
    _applied = True

    def _patched_build(subagents, task_description=None):  # type: ignore[no-untyped-def]
        wrapped = [
            {**spec, "runnable": _RetrySubagentRunnable(spec["runnable"], spec["name"], max_retries=max_retries)}
            for spec in subagents
        ]
        SUBAGENT_RUNNABLES.clear()
        SUBAGENT_RUNNABLES.update({spec["name"]: spec["runnable"] for spec in wrapped})
        ctx = subagent_invocation_ctx or _current_subagent_invocation_ctx()
        return _build_task_tool_forwarding_config(
            wrapped,
            task_description,
            subagent_invocation_ctx=ctx,
        )

    _deepagents_subagents._build_task_tool = _patched_build
