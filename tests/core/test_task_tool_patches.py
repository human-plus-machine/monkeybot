"""Tests for task tool RunnableConfig forwarding."""

from types import SimpleNamespace

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import var_child_runnable_config

from src.core.task_tool_patches import _invoke_config_for_subagent


class _StubHandler(BaseCallbackHandler):
    pass


def test_invoke_config_for_subagent_merges_context_callbacks() -> None:
    h = _StubHandler()
    token = var_child_runnable_config.set({"callbacks": [h]})
    try:
        runtime = SimpleNamespace(config={"configurable": {"memory_context_dir": "/tmp/x"}})
        merged = _invoke_config_for_subagent(runtime)  # type: ignore[arg-type]
        assert merged["configurable"]["memory_context_dir"] == "/tmp/x"
        cbs = merged.get("callbacks")
        assert cbs is not None
        if isinstance(cbs, list):
            assert h in cbs
        else:
            assert h in cbs.handlers
    finally:
        var_child_runnable_config.reset(token)


def test_invoke_config_for_subagent_preserves_runtime_configurable() -> None:
    token = var_child_runnable_config.set({})
    try:
        runtime = SimpleNamespace(config={"configurable": {"k": "v"}})
        merged = _invoke_config_for_subagent(runtime)  # type: ignore[arg-type]
        assert merged["configurable"]["k"] == "v"
    finally:
        var_child_runnable_config.reset(token)
