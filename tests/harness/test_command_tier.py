"""Unit tests for CommandTierMW."""

from __future__ import annotations

import pytest

from src.core.harness.errors import SandboxDenied
from src.core.harness.middleware.command_tier import CommandTierMW
from src.core.harness.specs import CommandTierSpec


def test_classify_preapproved() -> None:
    mw = CommandTierMW(CommandTierSpec(preapproved=["git status"], requires_approval=["git push"]))
    assert mw.classify("git status") == "preapproved"
    assert mw.classify("git status --short") == "preapproved"


def test_classify_requires_approval_default() -> None:
    mw = CommandTierMW(CommandTierSpec(preapproved=["git status"]))
    assert mw.classify("unknown cmd") == "requires_approval"


def test_classify_denied_takes_precedence() -> None:
    mw = CommandTierMW(
        CommandTierSpec(preapproved=["sudo"], denied=["sudo"])
    )
    assert mw.classify("sudo rm -rf /") == "denied"


def test_enforce_raises_on_denied() -> None:
    mw = CommandTierMW(CommandTierSpec(denied=["rm -rf /"]))
    with pytest.raises(SandboxDenied):
        mw.enforce("rm -rf /")
