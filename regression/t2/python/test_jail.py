"""T2-SHELL-17: an unavailable workspace jail fails open.

The jail is forced unavailable so the pin does not depend on this host having
seatbelt or user namespaces, and so a host that has them still exercises the
fail-open branch. The product skips the jail on those hosts; this test does
not skip.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import monkeybot.core.tools.core_tool_executor as executor_module
import monkeybot.core.tools.terminal as terminal_module
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.fs_isolation import IsolationSupport
from monkeybot.core.tools.terminal import TerminalExecutor
from testkit.context import _ctx


class _NoMCP:
    def split_prefixed_tool(self, name: str) -> None:
        del name
        return None


def _unavailable() -> IsolationSupport:
    return IsolationSupport("none", "test-unavailable")


@pytest.mark.case("T2-SHELL-17")
async def test_jail_fail_open_does_not_rewrite_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    host_cache = tmp_path / "host-uv-cache"
    host_cache.mkdir()
    sentinel = host_cache / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    monkeypatch.setenv("UV_CACHE_DIR", str(host_cache))
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    monkeypatch.setattr(executor_module, "isolation_support", _unavailable)
    monkeypatch.setattr(terminal_module, "isolation_support", _unavailable)
    monkeypatch.setattr(terminal_module, "_jail_unavailable_warned", False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    executor = CoreToolExecutor(
        workspace_root=workspace,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),  # type: ignore[arg-type]
    )
    assert isinstance(executor._terminal, TerminalExecutor)

    with caplog.at_level(logging.WARNING, logger="monkeybot.core.tools.terminal"):
        first = await executor.execute(
            call=ToolCall(
                call_id="c1",
                name="run_command",
                args={
                    "argv": ["python", "-c", "import os; print(os.environ.get('UV_CACHE_DIR', ''))"]
                },
            ),
            ctx=_ctx(workspace_root=workspace),
        )
        await executor.execute(
            call=ToolCall(
                call_id="c2",
                name="run_command",
                args={
                    "argv": ["python", "-c", "import os; print(os.environ.get('UV_CACHE_DIR', ''))"]
                },
            ),
            ctx=_ctx(workspace_root=workspace),
        )

    assert first.error is None
    body = json.loads(first.blocks[0].text)  # type: ignore[index]
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == str(host_cache)
    assert not (workspace / ".monkeybot" / "tool-cache").exists()
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    warnings = [
        record for record in caplog.records if "WITHOUT the workspace jail" in record.message
    ]
    assert len(warnings) == 1
