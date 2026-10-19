"""ProgressTracker edit-tool signals: every real write tool must be tracked."""

from __future__ import annotations

import pytest

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.hooks import HookEvent
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.match import WRITE_TOOLS
from monkeybot.core.verifier.tracker import ProgressTracker
from tests.core.test_progress_tracker import _payload


def _tracker() -> tuple[ProgressTracker, VerdictMailbox]:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=1),
    )
    return tracker, mailbox


def test_write_tools_name_real_tools() -> None:
    assert {"write_file", "replace_in_file", "apply_patch"} == WRITE_TOOLS


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_write_on_unread_path_fires_write_without_read(tool: str) -> None:
    tracker, mailbox = _tracker()
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="read_file",
            tool_args={"path": "a.py"},
        )
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name=tool,
            tool_args={"path": "b.py"},
        )
    )
    verdicts = mailbox.take_ready("t1")
    assert len(verdicts) == 1
    assert "write_without_read" in verdicts[0].triggering_signals


def test_replace_in_file_after_read_is_quiet() -> None:
    tracker, mailbox = _tracker()
    for tool in ("read_file", "replace_in_file"):
        tracker._observe_tool(
            _payload(
                event=HookEvent.POST_TOOL,
                tool_name=tool,
                tool_args={"path": "a.py"},
            )
        )
    assert mailbox.take_ready("t1") == []
