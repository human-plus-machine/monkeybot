"""Unit tests for IdentityLoader."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness.errors import HarnessConfigError
from src.core.harness.identity import IdentityLoader
from src.core.harness.specs import IdentitySpec


def test_enforce_rules_missing_raises(tmp_path: Path) -> None:
    spec = IdentitySpec(dir=str(tmp_path), enforce_rules=True)
    with pytest.raises(HarnessConfigError):
        IdentityLoader(spec).load()


def test_loads_present_files(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("soul body")
    (tmp_path / "RULES.md").write_text("- [R-1] DENY_TOOL: rm*")
    (tmp_path / "IDENTITY.md").write_text("role: test")
    spec = IdentitySpec(dir=str(tmp_path), enforce_rules=True)
    identity = IdentityLoader(spec).load()
    assert identity.soul == "soul body"
    assert "DENY_TOOL" in identity.rules
    assert "role: test" in identity.identity
    block = identity.system_prompt_block()
    assert "SOUL" in block and "RULES" in block
