"""T1-SKILL-01: skill discovery keeps immediate SKILL.md folders only."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.context import _discover_skills

_FRONTMATTER_WINS = """---
name: alpha
description: Front matter wins
---

# The first line loses

Body that is not the description.
"""


@pytest.mark.case("T1-SKILL-01")
def test_discover_immediate_subdirs_frontmatter_wins(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "SKILL.md").write_text(_FRONTMATTER_WINS, encoding="utf-8")
    nested = alpha / "nested"
    nested.mkdir()
    (nested / "SKILL.md").write_text(
        "---\ndescription: nested secret\n---\n\n# Nested skill\n",
        encoding="utf-8",
    )
    beta = root / "beta"
    beta.mkdir()
    (beta / "README.md").write_text("no skill file\n", encoding="utf-8")
    outer = root / "outer" / "inner"
    outer.mkdir(parents=True)
    (outer / "SKILL.md").write_text("# Only nested\n", encoding="utf-8")
    (root / "SKILL.md").write_text(
        "---\ndescription: root is not a skill\n---\n",
        encoding="utf-8",
    )
    (root / "notes.txt").write_text("ignore\n", encoding="utf-8")

    found = _discover_skills(root)
    by_name = {skill.name: skill.description for skill in found}

    assert set(by_name) == {"alpha"}
    assert by_name["alpha"] == "Front matter wins"
    assert by_name["alpha"] != "The first line loses"
    assert "nested secret" not in by_name["alpha"]
