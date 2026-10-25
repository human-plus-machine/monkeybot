"""Tests for CommandTierInspector and run_command policy YAML."""

from pathlib import Path

import pytest
import yaml

from monkeybot.core.context import TurnContext
from monkeybot.core.tools.inspector import (
    CommandTierConfigError,
    CommandTierInspector,
    Decision,
    DEFAULT_DENY_PATTERNS,
    InspectorToolCall,
    load_command_tier_policy,
)
from importlib import resources


def _minimal_ctx() -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


def _write_policy(tmp_path: Path) -> Path:
    """Copy packaged policy into tmp_path for isolated inspector construction."""
    content = (
        resources.files("monkeybot_cli.scaffold_defaults") / "command_allowlist.yaml"
    ).read_text(encoding="utf-8")
    dest = tmp_path / "policy.yaml"
    dest.write_text(content, encoding="utf-8")
    return dest


@pytest.mark.asyncio
async def test_inspector_python_skills_allowed_when_no_deny_match(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall(
        "1",
        "run_command",
        {"command": "python skills/foo/run.py"},
    )
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"
    assert d.message is None


@pytest.mark.asyncio
async def test_inspector_destructive_rm_denied_by_deny_pattern(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "rm -rf /"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message


@pytest.mark.asyncio
async def test_inspector_restricted_curl_denied(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "curl http://x"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_inspector_unknown_command_asks_with_grant_key(tmp_path: Path) -> None:
    """A binary outside every allowlist now asks (with a grant_key) instead of
    silently falling through to a later hard rejection at the executor."""
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "touch a"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "confirm"
    assert d.grant_key == "touch"
    assert d.grant_kind == "command"


@pytest.mark.asyncio
async def test_inspector_unknown_command_allowed_once_turn_grant(tmp_path: Path) -> None:
    """A binary already granted this turn (an earlier "Allow once"/"Always
    allow" click) is allowed without asking again."""
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "touch a"})
    ctx = _minimal_ctx()
    ctx.turn_command_grants.add("touch")
    d = await inspector.check(call, ctx)
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_unknown_command_allowed_via_durable_grant(tmp_path: Path) -> None:
    """A binary durably granted via ``grants.json`` is allowed without asking."""
    p = _write_policy(tmp_path)
    grants_path = tmp_path / "grants.json"
    from monkeybot.core.tools.grant_store import add_command_grant

    add_command_grant(grants_path, command="touch", created_at="2026-01-01T00:00:00+00:00")
    inspector = CommandTierInspector(p, grants_path=grants_path)
    call = InspectorToolCall("1", "run_command", {"command": "touch a"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_argv_used_for_deny_check(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"argv": ["curl", "http://x"]})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_inspector_argv_json_string_coerced_for_deny_check(tmp_path: Path) -> None:
    """Stringified argv (LLM quirk) still reaches deny-pattern checks."""
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall(
        "1",
        "run_command",
        {"argv": '["curl", "http://x"]'},
    )
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message == "denied by run_command deny_patterns policy"


@pytest.mark.asyncio
async def test_inspector_argv_invalid_string_clear_error(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"argv": "git status"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message is not None
    assert "argv must be an array" in d.message


@pytest.mark.asyncio
async def test_inspector_non_run_command_bypassed(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "read_file", {"path": "x"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_invalid_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("[broken", encoding="utf-8")
    with pytest.raises(CommandTierConfigError, match="invalid YAML"):
        CommandTierInspector(bad)


def test_inspector_obsolete_tier_order_raises(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tier_order": [{"name": "x", "action": "allow", "patterns": [".*"]}],
                "default": "allow",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="obsolete key"):
        CommandTierInspector(path)


def test_decision_confirm_variant_constructible() -> None:
    d = Decision(kind="confirm", message=None)
    assert d.kind == "confirm"
    assert d.message is None


def test_load_policy_omitted_allowlists_use_terminal_defaults(tmp_path: Path) -> None:
    from monkeybot.core.tools.terminal import ALLOWED_COMMANDS, ALLOWED_PATHS

    path = tmp_path / "t.yaml"
    path.write_text("allowed_commands:\n  - echo\n", encoding="utf-8")
    policy = load_command_tier_policy(path)
    assert policy.allowed_commands == ("echo",)
    assert policy.allowed_path_prefixes == tuple(ALLOWED_PATHS)
    assert policy.deny_patterns == DEFAULT_DENY_PATTERNS


def test_load_policy_explicit_empty_deny_patterns_opt_out(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text("deny_patterns: []\n", encoding="utf-8")
    policy = load_command_tier_policy(path)
    assert policy.deny_patterns == ()


def test_load_policy_empty_allowed_commands_raises(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "allowed_commands": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="allowed_commands"):
        load_command_tier_policy(path)


def test_load_policy_invalid_deny_regex_raises(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "deny_patterns": ["("],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="invalid regex"):
        CommandTierInspector(path)


@pytest.mark.parametrize(
    "args",
    [
        {"argv": ["uv", "pip", "install", "google-genai"]},
        {"argv": ["pip", "install", "requests"]},
        {"argv": ["python3", "-m", "pip", "install", "foo"]},
        {"argv": ["bash", "-c", "pip install foo"]},
        {"argv": ["npm", "install", "lodash"]},
        {"argv": ["apt-get", "install", "curl"]},
    ],
)
@pytest.mark.asyncio
async def test_inspector_install_commands_denied(tmp_path: Path, args: dict) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", args)
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message


@pytest.mark.asyncio
async def test_inspector_uv_run_allowed(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"argv": ["uv", "run", "pytest", "tests/"]})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_skill_script_argv_allowed(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall(
        "1",
        "run_command",
        {
            "argv": [
                "python3",
                "./skills/image-generator/generate_image.py",
                "--prompt",
                "test",
            ]
        },
    )
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


def _anchored_policy(tmp_path: Path) -> Path:
    path = tmp_path / "anchored.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "allowed_commands": ["bash", "sh", "ls", "echo", "rm", "python3"],
                "deny_patterns": [r"^rm\s", r"(^|\s)uv\s+add"],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"argv": ["bash", "-c", "rm -rf build"]},
        {"argv": ["bash", "-lc", "ls && rm -rf build"]},
        {"argv": ["sh", "-c", "echo hi;rm -rf build"]},
        {"argv": ["bash", "-c", "uv add requests"]},
        {"argv": ["bash", "-c", "ls\nrm x"]},
        {"argv": ["bash", "-c", "bash -c 'rm -rf build'"]},
        {"command": "bash -c 'rm -rf build'"},
    ],
)
async def test_inspector_denies_commands_hidden_in_shell_c(
    tmp_path: Path, args: dict[str, object]
) -> None:
    inspector = CommandTierInspector(_anchored_policy(tmp_path))
    d = await inspector.check(InspectorToolCall("1", "run_command", args), _minimal_ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"argv": ["bash", "-c", "ls && echo done"]},
        {"argv": ["bash", "-c", "echo 'rm -rf is dangerous'"]},
        {"argv": ["bash", "script.sh"]},
    ],
)
async def test_inspector_allows_benign_shell_c(tmp_path: Path, args: dict[str, object]) -> None:
    inspector = CommandTierInspector(_anchored_policy(tmp_path))
    d = await inspector.check(InspectorToolCall("1", "run_command", args), _minimal_ctx())
    assert d.kind == "allow"


# --- Findings 1-3: commands hidden inside bash -c ---------------------------------


def _shell_c_inspector(tmp_path: Path) -> CommandTierInspector:
    path = tmp_path / "anchored.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "allowed_commands": ["bash", "sh", "env", "timeout", "ls", "echo", "rm"],
                "deny_patterns": [r"^rm\s+-rf"],
            }
        ),
        encoding="utf-8",
    )
    return CommandTierInspector(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        # 1: empty quoted token treated as an operator
        ["bash", "-c", 'rm "" -rf build'],
        # 2: option values and wrapper commands
        ["bash", "-o", "pipefail", "-c", "rm -rf build"],
        ["env", "bash", "-c", "rm -rf build"],
        ["timeout", "5", "bash", "-c", "rm -rf build"],
        # 3: keywords, grouping, command substitution
        ["bash", "-c", "if true; then rm -rf build; fi"],
        ["bash", "-c", "{ rm -rf build; }"],
        ["bash", "-c", "echo `rm -rf build`"],
        ["bash", "-c", 'echo "$(rm -rf build)"'],
    ],
)
async def test_inspector_shell_c_bypasses_are_denied(tmp_path: Path, argv: list[str]) -> None:
    d = await _shell_c_inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": argv}), _minimal_ctx()
    )
    assert d.kind == "deny", argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "ls && echo done"],
        ["bash", "-c", "echo 'rm -rf is dangerous'"],
        ["bash", "-o", "pipefail", "-c", "ls | echo ok"],
        ["env", "FOO=1", "ls"],
    ],
)
async def test_inspector_benign_shell_c_still_allowed(tmp_path: Path, argv: list[str]) -> None:
    d = await _shell_c_inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": argv}), _minimal_ctx()
    )
    assert d.kind == "allow", argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", 'echo "a\nb"; rm -rf build'],
        ["bash", "-c", "eval 'rm -rf build'"],
        ["bash", "-c", "find . -exec rm -rf {} \\;"],
        ["find", ".", "-exec", "rm", "-rf", "x", ";"],
    ],
)
async def test_inspector_denies_multiline_quote_eval_and_find_exec(
    tmp_path: Path, argv: list[str]
) -> None:
    d = await _shell_c_inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": argv}), _minimal_ctx()
    )
    assert d.kind == "deny", argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script",
    ["echo hi > rm-notes.txt", "ls 2>&1", "echo '$(rm -rf x)'"],
)
async def test_inspector_redirects_and_single_quoted_substitution_are_not_commands(
    tmp_path: Path, script: str
) -> None:
    d = await _shell_c_inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": ["bash", "-c", script]}), _minimal_ctx()
    )
    assert d.kind == "allow", script


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script",
    [
        "cat > f <<EOF\nhello\nEOF",
        "bash <<EOF\nrm -rf build\nEOF",
        "echo $'it\\'s'; rm -rf build",
        "echo 'unterminated\nrm -rf build",
        "eval 'rm -rf build'",
    ],
)
async def test_inspector_fails_closed_on_unscreenable_scripts(tmp_path: Path, script: str) -> None:
    d = await _shell_c_inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": ["bash", "-c", script]}), _minimal_ctx()
    )
    assert d.kind == "deny", script


@pytest.mark.asyncio
async def test_inspector_unscreenable_script_allowed_without_deny_patterns(tmp_path: Path) -> None:
    path = tmp_path / "nodeny.yaml"
    path.write_text(
        yaml.safe_dump({"allowed_commands": ["bash", "cat"], "deny_patterns": []}),
        encoding="utf-8",
    )
    d = await CommandTierInspector(path).check(
        InspectorToolCall("1", "run_command", {"argv": ["bash", "-c", "cat <<EOF\nhi\nEOF"]}),
        _minimal_ctx(),
    )
    assert d.kind == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["bash", "-c", "/bin/rm -rf build"], "deny"),
        (["bash", "-c", "env /usr/bin/rm -rf build"], "deny"),
        (["/bin/rm", "-rf", "build"], "deny"),
        (["bash", "-c", "ls ./tools/rm-notes && echo ok"], "allow"),
        (["bash", "-c", 'echo "\\$(rm -rf x)"'], "allow"),
    ],
)
async def test_inspector_system_binary_paths_and_escaped_substitution(
    tmp_path: Path, argv: list[str], expected: str
) -> None:
    d = await _shell_c_inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": argv}), _minimal_ctx()
    )
    assert d.kind == expected, argv
