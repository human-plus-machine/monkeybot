"""Spikes for the PR #308 review findings. Each test encodes the reported failure scenario."""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest
import yaml

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import CommandTierInspector, InspectorToolCall
from monkeybot.core.tools.patch import PatchError, UpdateChunk, derive_new_contents, parse_patch
from monkeybot.core.tools.spill_inventory import shape_run_command_streams
from monkeybot.core.tools.types import unwrap_tool_execution_result
from tests.core.test_core_tool_executor import _ctx, _mem_sub, _NoMCP
from tests.core.test_inspector import _minimal_ctx

# --- Findings 1-3: commands hidden inside bash -c ---------------------------------


def _inspector(tmp_path: Path) -> CommandTierInspector:
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
async def test_spike_shell_c_bypasses_are_denied(tmp_path: Path, argv: list[str]) -> None:
    d = await _inspector(tmp_path).check(
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
async def test_spike_benign_shell_c_still_allowed(tmp_path: Path, argv: list[str]) -> None:
    d = await _inspector(tmp_path).check(
        InspectorToolCall("1", "run_command", {"argv": argv}), _minimal_ctx()
    )
    assert d.kind == "allow", argv


# --- Finding 4: configured path prefixes widened to the whole workspace -----------


def _executor(tmp_path: Path, **kwargs: object) -> CoreToolExecutor:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        **kwargs,  # type: ignore[arg-type]
    )


async def _run(ex: CoreToolExecutor, argv: list[str]) -> tuple[str | None, str | None]:
    return unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="spike", name="run_command", args={"argv": argv}),
            ctx=_ctx(),
        )
    )


@pytest.mark.asyncio
async def test_spike_configured_path_prefixes_are_not_widened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "code").mkdir()
    (tmp_path / "secret.txt").write_text("s3cret\n", encoding="utf-8")
    ex = _executor(
        tmp_path,
        run_command_allowed_commands=["cat"],
        run_command_allowed_path_prefixes=["./code/"],
    )
    out, err = await _run(ex, ["cat", "./secret.txt"])
    assert out is None and err is not None, out
    assert json.loads(err)["error_kind"] == "policy"


# --- Finding 5: shaped JSON can still exceed the inline hard cap ------------------


@pytest.mark.asyncio
async def test_spike_control_char_output_stays_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ex = _executor(tmp_path)
    script = "for i in range(3000):\n    print(str(i) + '\\x01' * 200)\nprint('SUMMARY-END')\n"
    out, err = await _run(ex, ["python3", "-c", script])
    assert err is None and out is not None
    payload = json.loads(out)  # a cap cut mid-JSON fails here
    assert payload["exit_code"] == 0
    assert "SUMMARY-END" in payload["stdout_tail"]
    assert "note" in payload


# --- Finding 6: a failed spill write fails a finished command ---------------------


@pytest.mark.asyncio
async def test_spike_spill_write_error_keeps_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    real_write_text = pathlib.Path.write_text

    def failing_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if ".monkeybot" in self.parts and "spill" in self.parts:
            raise OSError(28, "No space left on device")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pathlib.Path, "write_text", failing_write_text)
    ex = _executor(tmp_path)
    script = "print('x' * 200000)\nprint('DONE-TAIL')\n"
    out, err = await _run(ex, ["python3", "-c", script])
    assert err is None and out is not None, err
    payload = json.loads(out)
    assert payload["exit_code"] == 0
    assert "output_path" not in payload
    assert "DONE-TAIL" in payload["stdout_tail"]


# --- Finding 7: an ambiguous @@ header silently picks the first match --------------

_TWO_INITS = (
    "class A:\n    def __init__(self):\n        pass\n"
    "class B:\n    def __init__(self):\n        pass\n"
)


def test_spike_ambiguous_context_header_rejected() -> None:
    chunks = (
        UpdateChunk(
            old_lines=("        pass",),
            new_lines=("        self.x = 1",),
            change_context="def __init__(self):",
        ),
    )
    with pytest.raises(PatchError) as ei:
        derive_new_contents("f.py", chunks, _TWO_INITS)
    assert ei.value.code == "ambiguous_context"


def test_spike_header_ambiguous_but_body_unique_still_applies() -> None:
    original = "def f():\n    a = 1\ndef f():\n    b = 2\n"
    chunks = (
        UpdateChunk(old_lines=("    b = 2",), new_lines=("    b = 3",), change_context="def f():"),
    )
    assert (
        derive_new_contents("f.py", chunks, original)
        == "def f():\n    a = 1\ndef f():\n    b = 3\n"
    )


# --- Finding 8: trailing blanks dropped before *** End of File ---------------------


def test_spike_end_of_file_chunk_keeps_trailing_blank() -> None:
    text = "*** Begin Patch\n*** Update File: a.txt\n@@\n-x\n+y\n\n*** End of File\n*** End Patch\n"
    chunk = parse_patch(text)[0].chunks[0]  # type: ignore[union-attr]
    out = derive_new_contents("a.txt", (chunk,), "x\nmid\nx\n\n")
    assert out.startswith("x\nmid\ny"), out


# --- Finding 9: stdout shown twice when it is small --------------------------------


def test_spike_small_stdout_not_duplicated() -> None:
    shaped = shape_run_command_streams("short-out\n", "e\n" * 50_000, unit_chars=500)
    assert shaped["stdout_tail"].strip() == "short-out"
    assert not shaped.get("stdout_head"), shaped["stdout_head"]
