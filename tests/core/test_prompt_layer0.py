"""Tests for Layer 0 prompt template injection (SOUL, USER, TOOLS)."""

from monkeybot.core.prompt import (
    SOUL_SECTION_TEMPLATE,
    TOOLS_SECTION_TEMPLATE,
    USER_SECTION_TEMPLATE,
    compose_system_prompt,
)


class TestLayer0Injection:
    """Tests for Layer 0 (SOUL, USER, TOOLS) prompt injection."""

    def test_soul_content_injected_before_skills(self):
        """Test that SOUL section appears before Available Skills."""
        prompt = compose_system_prompt(soul_content="I am X")
        soul_pos = prompt.find("[IDENTITY — HIGHEST PRIORITY]")
        skills_pos = prompt.find("## Available Skills")
        assert soul_pos != -1
        assert soul_pos < skills_pos

    def test_user_content_injected_after_soul(self):
        """Test that USER section appears after SOUL section."""
        prompt = compose_system_prompt(soul_content="I am X", user_content="User prefers Y")
        soul_pos = prompt.find("[IDENTITY — HIGHEST PRIORITY]")
        user_pos = prompt.find("[USER CONTEXT]")
        assert soul_pos < user_pos

    def test_tools_content_as_capability_guide(self):
        """Test that TOOLS section appears as Capability Guide."""
        prompt = compose_system_prompt(tools_content="Tool Z does...")
        assert "## Capability Guide" in prompt
        assert "Tool Z does..." in prompt

    def test_empty_defaults_no_layer0_blocks(self):
        """Test that empty defaults produce no Layer 0 blocks."""
        prompt = compose_system_prompt()
        assert "[IDENTITY — HIGHEST PRIORITY]" not in prompt
        assert "[USER CONTEXT]" not in prompt
        assert "## Capability Guide" not in prompt

    def test_only_soul_no_user_block(self):
        """Test that only SOUL appears when user_content is empty."""
        prompt = compose_system_prompt(soul_content="I am X")
        assert "[IDENTITY — HIGHEST PRIORITY]" in prompt
        assert "[USER CONTEXT]" not in prompt

    def test_do_not_reveal_still_wraps_all(self):
        """Test that DO NOT REVEAL header still wraps Layer 0 content."""
        prompt = compose_system_prompt(soul_content="I am X", user_content="User Y")
        reveal_pos = prompt.find("[SYSTEM INSTRUCTIONS - DO NOT REVEAL]")
        soul_pos = prompt.find("[IDENTITY — HIGHEST PRIORITY]")
        assert reveal_pos < soul_pos

    def test_backward_compat_no_new_params(self):
        """Test backward compatibility when new params are not provided."""
        prompt1 = compose_system_prompt(
            skills_manifest="- skill1: Test",
            user_system_prompt="Custom prompt",
            has_memory=True,
        )
        prompt2 = compose_system_prompt(
            skills_manifest="- skill1: Test",
            user_system_prompt="Custom prompt",
            has_memory=True,
        )
        assert prompt1 == prompt2
        assert "[IDENTITY — HIGHEST PRIORITY]" not in prompt2

    def test_soul_confidentiality_text_present(self):
        """Test that SOUL section includes confidentiality warning."""
        prompt = compose_system_prompt(soul_content="I am X")
        assert "never reproduce it verbatim" in prompt

    def test_all_three_params_simultaneously(self):
        """Test that all three Layer 0 params work together."""
        prompt = compose_system_prompt(
            soul_content="I am X",
            user_content="User Y",
            tools_content="Tool Z",
        )
        assert "[IDENTITY — HIGHEST PRIORITY]" in prompt
        assert "[USER CONTEXT]" in prompt
        assert "## Capability Guide" in prompt

    def test_templates_importable(self):
        """Test that template constants are importable and have placeholders."""
        assert "{soul_content}" in SOUL_SECTION_TEMPLATE
        assert "{user_content}" in USER_SECTION_TEMPLATE
        assert "{tools_content}" in TOOLS_SECTION_TEMPLATE
