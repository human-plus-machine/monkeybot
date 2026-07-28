"""Window-derived soft spill and unified read_file char budgets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.context_budget import ContextBudgeter
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.spill_inventory import (
    spill_budgets_from_window,
    spill_inline_and_note,
    write_spill_with_inventory,
)
from monkeybot.core.tools.types import unwrap_tool_execution_result
from monkeybot.core.tools.workspace_service import WorkspaceFileService
from monkeybot.core.types.content_blocks import Text, ToolResponse
from monkeybot.core.workspace import create_workspace_storage


def _mem_sub(root: Path) -> MemorySubsystem:
    p = Path(root)
    p.mkdir(exist_ok=True)
    uri = "local://" + str(p.resolve())
    fake = ScriptedFakeProvider(
        [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
    )
    return MemorySubsystem(
        storage=create_workspace_storage(uri),
        provider=fake,
        model="gemini-2.5-flash",
        memory_uri=uri,
    )


class _NoMCP:
    async def connect(self, name: str, command: str, args: list[str], env: dict[str, str]):
        del name, command, args, env
        return []

    async def connect_streamable_http(self, name: str, url: str, headers: dict[str, str] | None = None):
        del name, url, headers
        return []

    async def disconnect(self, name: str) -> None:
        del name

    async def call_tool(self, server_name: str, tool_name: str, args: dict[str, object]) -> str:
        del server_name, tool_name, args
        return ""

    def all_tools(self):
        return []

    def catalog_names(self):
        return []

    def known_server_names(self):
        return []

    def is_connected(self, name: str) -> bool:
        del name
        return False

    def split_prefixed_tool(self, prefixed_name: str):
        del prefixed_name
        return None

    async def connect_from_catalog(self, name: str):
        del name
        return []

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path, raise_on_error


def _ctx(*, context_window_tokens: int = 200_000, skills: list[SkillRef] | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=skills or [],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        context_window_tokens=context_window_tokens,
    )


@pytest.mark.parametrize("window", [1, 2, 3, 8_000, 128_000, 200_000, 1_000_000])
def test_spill_budgets_scale_and_order(window: int) -> None:
    b = spill_budgets_from_window(window)
    assert b.spill_threshold < b.inline_budget < b.spill_read_budget
    if window < 8_000:
        # Tiny / misconfigured windows must still return ordered budgets.
        return
    window_chars = window * 4
    assert b.inline_budget <= max(16_000, int(window_chars * 0.15) + 1)
    if window == 8_000:
        # No single result should exceed ~15% of window_chars inline.
        assert b.inline_budget <= window_chars * 0.15 + 1


def test_soft_spill_inlines_body_without_duplicate_preview() -> None:
    body = "line\n" * 5_000
    prefix, note = spill_inline_and_note(
        body, ".monkeybot/spill/t/c.txt", tool_name="run_command", inline_budget=8_000
    )
    assert prefix.startswith("line")
    assert len(prefix) == 8_000
    assert "Preview:" not in note
    assert "Spill inventory" in note
    assert ".monkeybot/spill/t/c.txt" in note


def test_soft_spill_preview_when_inline_budget_zero() -> None:
    body = "x" * 1000
    prefix, note = spill_inline_and_note(
        body, ".monkeybot/spill/t/c.txt", tool_name="run_command", inline_budget=0
    )
    assert prefix == ""
    assert "Preview:" in note


def test_write_spill_soft_keeps_body_and_file(tmp_path: Path) -> None:
    body = "y" * 25_000
    out = write_spill_with_inventory(
        body, tmp_path, "th1", "call-1", tool_name="run_command", inline_budget=10_000
    )
    spill = tmp_path / ".monkeybot" / "spill" / "th1" / "call-1.txt"
    assert spill.read_text(encoding="utf-8") == body
    assert out.startswith("y" * 100)
    assert "Spill inventory" in out
    assert "Preview:" not in out


@pytest.mark.asyncio
async def test_million_window_40k_result_never_truncated_without_file() -> None:
    """Invariant 1: at 1M window, 40k result is below threshold — never bare-chopped."""
    from monkeybot.core.context.tool_result_ingress import cap_tool_result_text

    budgets = spill_budgets_from_window(1_000_000)
    assert budgets.spill_threshold > 40_000
    body = "z" * 40_000
    hard_cap = budgets.inline_hard_cap
    assert hard_cap >= budgets.spill_threshold
    capped = cap_tool_result_text(body, max_chars=hard_cap)
    assert capped == body


@pytest.mark.asyncio
async def test_run_command_soft_spill_survives_budgeter_at_low_pressure(tmp_path: Path) -> None:
    body = "LOG line\n" * 8_000
    note_path = ".monkeybot/spill/t/c.txt"
    prefix, note = spill_inline_and_note(
        body, note_path, tool_name="run_command", inline_budget=20_000
    )
    history = f"{prefix}\n{note}"
    block = ToolResponse(id="c1", tool_name="run_command", result=[Text(text=history)])
    budgeter = ContextBudgeter.for_window(window_tokens=200_000, used_tokens=5_000)
    assert budgeter.pressure_tier is None or budgeter.pressure_tier == "light"
    out_blocks, _ = budgeter.fit_content_blocks([block])
    text = out_blocks[0].result[0].text  # type: ignore[union-attr]
    assert "Spill inventory" in text
    assert note_path in text
    assert "Full output at:" in text


@pytest.mark.asyncio
async def test_ordinary_read_end_line_matches_content(tmp_path: Path) -> None:
    """Invariant 2: no post-hoc chop — content lines match reported end_line."""
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    # ~80k chars of numbered-ish content (2000 lines * ~40 chars).
    lines = [f"{'word' * 10}-{i}" for i in range(2500)]
    (root / "big.py").write_text("\n".join(lines), encoding="utf-8")
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP())
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="rf", name="read_file", args={"path": "big.py"}),
            ctx=_ctx(context_window_tokens=200_000),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    content_lines = payload["content"].splitlines()
    reported = payload["end_line"] - payload["start_line"] + 1
    assert len(content_lines) == reported
    assert "truncated" not in payload["content"].lower() or payload["truncated"] is True
    # Encoded payload under read budget.
    budgets = spill_budgets_from_window(200_000)
    assert len(out) <= budgets.spill_read_budget


@pytest.mark.asyncio
async def test_spill_path_read_chars_only_no_default_line_limit(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    spill = root / ".monkeybot" / "spill" / "t" / "big.txt"
    spill.parent.mkdir(parents=True)
    spill.write_text("\n".join(f"line{i}" for i in range(3000)), encoding="utf-8")
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP())
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="r1",
                name="read_file",
                args={"path": ".monkeybot/spill/t/big.txt"},
            ),
            ctx=_ctx(context_window_tokens=200_000),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    # Chars-only: can exceed the ordinary 2000-line default when window allows.
    n = payload["end_line"] - payload["start_line"] + 1
    assert n > 2000 or payload["truncated"] is False
    assert len(out) > 32_768 or n > 2000


def test_read_file_paging_next_offset_no_gaps(tmp_path: Path) -> None:
    svc = WorkspaceFileService(tmp_path)
    lines = [f"L{i}-{'x' * 80}" for i in range(500)]
    (tmp_path / "src.txt").write_text("\n".join(lines), encoding="utf-8")
    seen: list[int] = []
    offset = 1
    while True:
        result = svc.read_file("src.txt", offset=offset, limit=None, max_chars=5_000, apply_default_limit=False)
        for i in range(result["start_line"], result["end_line"] + 1):
            seen.append(i)
        if not result["truncated"]:
            break
        assert "next_offset" in result
        assert result["next_offset"] == result["end_line"] + 1
        offset = result["next_offset"]
    assert seen == list(range(1, 501))


def test_single_line_longer_than_budget_advances(tmp_path: Path) -> None:
    svc = WorkspaceFileService(tmp_path)
    (tmp_path / "long.txt").write_text("A" * 10_000 + "\nB\nC\n", encoding="utf-8")
    result = svc.read_file("long.txt", offset=1, max_chars=100, apply_default_limit=False)
    assert result["end_line"] == 1
    assert result["truncated"] is True
    assert result["next_offset"] == 2
    second = svc.read_file("long.txt", offset=2, max_chars=100, apply_default_limit=False)
    assert second["start_line"] == 2
    assert "B" in second["content"]


def test_complete_read_is_not_reported_truncated(tmp_path: Path) -> None:
    """Invariant 2: a read that reaches EOF must not claim truncation."""
    svc = WorkspaceFileService(tmp_path)
    (tmp_path / "small.txt").write_text(
        "\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8"
    )
    for kwargs in (
        {"max_chars": 200_000},
        {"max_chars": 200_000, "limit": 500},
        {"max_chars": None},
    ):
        result = svc.read_file("small.txt", offset=1, **kwargs)  # type: ignore[arg-type]
        assert result["end_line"] == 100
        assert result["truncated"] is False, kwargs
        assert "next_offset" not in result, kwargs


def test_hard_sliced_line_is_marked_and_omits_dead_next_offset(tmp_path: Path) -> None:
    svc = WorkspaceFileService(tmp_path)
    (tmp_path / "tail.txt").write_text("short\n" + "X" * 5_000, encoding="utf-8")
    result = svc.read_file("tail.txt", offset=2, max_chars=500, apply_default_limit=False)
    assert result["truncated"] is True
    assert "line cut at char budget" in result["content"]
    # Last line: the remainder is unreachable via offset, so do not point past EOF.
    assert "next_offset" not in result


def test_oversized_json_inlines_valid_json(tmp_path: Path) -> None:
    del tmp_path
    payload = json.dumps(
        {"items": [{"id": i, "name": f"row-{i}", "blob": "q" * 60} for i in range(4_000)]}
    )
    budget = 40_000
    assert len(payload) > budget
    body, note = spill_inline_and_note(
        payload, ".monkeybot/spill/t/c.txt", tool_name="mcp__x", inline_budget=budget
    )
    assert len(body) <= budget
    json.loads(body)  # must stay parseable, not a raw prefix
    assert "(+" in body and "more items)" in body
    assert "array-capped" in note
    assert ".monkeybot/spill/t/c.txt" in note


def test_summary_cap_scales_with_window() -> None:
    from monkeybot.core.context.tool_result_ingress import summarize_tool_result_text

    text = "z" * 200_000
    small = len(summarize_tool_result_text(text, window_tokens=8_000))
    large = len(summarize_tool_result_text(text, window_tokens=1_000_000))
    assert small < large
    assert small == pytest.approx(
        spill_budgets_from_window(8_000).summary_max_chars, abs=200
    )


@pytest.mark.asyncio
async def test_env_vars_do_not_change_sizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    for key, val in (
        ("MONKEYBOT_SPILL_MIN_CHARS", "1"),
        ("MONKEYBOT_SPILL_READ_MAX_LINES", "1"),
        ("MONKEYBOT_TOOL_RESULT_MAX_CHARS", "100"),
        ("MONKEYBOT_READ_MAX_LINES", "1"),
        ("MONKEYBOT_READ_DEFAULT_LINES", "1"),
    ):
        monkeypatch.setenv(key, val)

    (root / "wide.txt").write_text("\n".join(f"L{i}" for i in range(50)), encoding="utf-8")
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP())
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="r", name="read_file", args={"path": "wide.txt"}),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    # Env says default_lines=1, but YAML-only config still uses code default 2000.
    assert payload["end_line"] - payload["start_line"] + 1 > 1

    small = "x" * 100
    big_skills = [SkillRef(name="s", description=small)]
    out2, err2 = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="ls", name="list_skills", args={}),
            ctx=_ctx(skills=big_skills),
        )
    )
    assert err2 is None and out2 is not None
    # Env says spill at 1 char — must NOT spill a tiny result.
    assert "Spill inventory" not in out2
    assert not (root / ".monkeybot" / "spill").exists()


@pytest.mark.asyncio
async def test_yaml_read_defaults_still_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.tools.core_tool_executor import workspace_settings_from_config

    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "tools:\n  read_default_lines: 7\n  read_max_lines: 50\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONKEYBOT_CONFIG", str(cfg_dir / "monkeybot.yaml"))
    monkeypatch.setenv("MONKEYBOT_READ_DEFAULT_LINES", "1")
    workspace_settings_from_config.cache_clear()
    settings = workspace_settings_from_config()
    assert settings.WORKSPACE_READ_DEFAULT_LINES == 7
    assert settings.WORKSPACE_READ_MAX_LINES == 50
    # Absurd env must not override YAML.
    assert settings.WORKSPACE_READ_DEFAULT_LINES != 1
    workspace_settings_from_config.cache_clear()
