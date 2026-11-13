"""``prompt.section_caps`` wiring."""

from __future__ import annotations

from types import SimpleNamespace

from monkeybot.core.config.settings import (
    PromptConfig,
    PromptSectionCapsConfig,
    prompt_config_from_section,
)
from monkeybot.core.prompts.section_budget import (
    SECTION_CHAR_CAPS,
    effective_section_char_caps,
    runtime_notes_char_cap,
)


def test_prompt_section_caps_default_runtime_notes() -> None:
    cfg = prompt_config_from_section({})
    assert cfg.section_caps.runtime_notes == SECTION_CHAR_CAPS["runtime_notes"]


def test_prompt_section_caps_overrides_runtime_notes() -> None:
    cfg = prompt_config_from_section({"section_caps": {"runtime_notes": 20_000}})
    assert cfg.section_caps.runtime_notes == 20_000


def test_effective_section_char_caps_reads_runtime_config() -> None:
    cfg = SimpleNamespace(
        prompt=PromptConfig(section_caps=PromptSectionCapsConfig(runtime_notes=12_345))
    )
    assert runtime_notes_char_cap(cfg) == 12_345
    caps = effective_section_char_caps(cfg)
    assert caps["runtime_notes"] == 12_345
    assert caps["todos"] == SECTION_CHAR_CAPS["todos"]


def test_runtime_notes_cap_without_config_uses_default() -> None:
    assert runtime_notes_char_cap(None) == SECTION_CHAR_CAPS["runtime_notes"]
