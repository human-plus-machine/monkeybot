from __future__ import annotations

import pytest

from monkeybot.core.subagent_registry import SubagentRegistry

REGISTRY_BLOCK = {
    "researcher": {
        "script": "subagents/researcher.py",
        "description": "Searches the web and summarizes findings on a given topic.",
        "model": "gemini-2.0-pro",
        "skills_path": ".agents/skills/research",
        "timeout_seconds": 120,
    },
    "reviewer": {
        "script": "subagents/reviewer.py",
        "description": "Reviews a draft for clarity.",
    },
}


def make_registry(block=None, **overrides):
    return SubagentRegistry(
        block if block is not None else REGISTRY_BLOCK,
        bot_skills_path=".agents/skills",
        bot_model="gemini-2.0-flash",
        global_timeout=300,
        **overrides,
    )


def test_resolve_known_name() -> None:
    r = make_registry()
    d = r.resolve("researcher")
    assert d.name == "researcher"
    assert d.model == "gemini-2.0-pro"
    assert d.timeout_seconds == 120
    assert d.skills_path == ".agents/skills/research"


def test_resolve_unknown_name() -> None:
    r = make_registry()
    with pytest.raises(KeyError) as exc_info:
        r.resolve("unknown")
    msg = str(exc_info.value)
    assert "unknown" in msg
    assert "researcher" in msg


def test_fallback_to_bot_defaults() -> None:
    r = make_registry()
    d = r.resolve("reviewer")
    assert d.model == "gemini-2.0-flash"
    assert d.skills_path == ".agents/skills"


def test_to_prompt_block_contains_names_and_header() -> None:
    block = make_registry().to_prompt_block()
    assert "researcher" in block
    assert "reviewer" in block
    assert "## Available Subagents" in block
    assert "Searches the web" in block


def test_to_prompt_block_empty() -> None:
    assert SubagentRegistry({}, bot_skills_path="x", bot_model="y").to_prompt_block() == ""


def test_validate_missing_script(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = make_registry()
    errors = r.validate()
    assert len(errors) == 2
    assert any("researcher" in e for e in errors)
    assert any("subagents/researcher.py" in e for e in errors)


def test_validate_all_present(tmp_path, monkeypatch) -> None:
    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "researcher.py").write_text("# stub")
    (tmp_path / "subagents" / "reviewer.py").write_text("# stub")
    monkeypatch.chdir(tmp_path)
    r = make_registry()
    assert r.validate() == []


def test_invalid_name_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="BadName"):
        SubagentRegistry(
            {"BadName": {"script": "x.py", "description": "d"}},
            bot_skills_path="x",
            bot_model="y",
        )


def test_name_with_space_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="my agent"):
        SubagentRegistry(
            {"my agent": {"script": "x.py", "description": "d"}},
            bot_skills_path="x",
            bot_model="y",
        )


def test_all_definitions_returns_copy() -> None:
    r = make_registry()
    defs = r.all_definitions()
    defs.clear()
    assert len(r.all_definitions()) == 2


def test_missing_description_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="description"):
        SubagentRegistry(
            {"my-agent": {"script": "x.py", "description": ""}},
            bot_skills_path="x",
            bot_model="y",
        )


def test_missing_script_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="script"):
        SubagentRegistry(
            {"my-agent": {"description": "Some description"}},
            bot_skills_path="x",
            bot_model="y",
        )
