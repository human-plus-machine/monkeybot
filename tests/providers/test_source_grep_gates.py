"""CI-style grep gates: banned legacy placeholder / sanitizer symbols in src."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _rg_no_matches(args: list[str]) -> None:
    assert _REPO_ROOT.name and (_REPO_ROOT / "pyproject.toml").is_file()
    try:
        proc = subprocess.run(
            ["rg", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("ripgrep (rg) is required in CI for grep-gate tests; install it in the workflow.")
        pytest.skip("ripgrep (rg) not installed")
    if proc.returncode == 1:
        return
    if proc.returncode != 0:
        raise AssertionError(f"rg failed ({proc.returncode}): {proc.stderr or proc.stdout}")
    matches = (proc.stdout or proc.stderr or "").strip()
    pytest.fail(f"grep gate found forbidden matches:\n{matches}")


def test_no_legacy_tool_placeholder_parsers_in_src() -> None:
    _rg_no_matches(
        [
            "-n",
            "_split_assistant_placeholder|_parse_tool_placeholder|_assistant_tool_placeholder",
            "src/monkeybot",
        ]
    )


def test_no_strip_tool_call_echo_in_src() -> None:
    _rg_no_matches(
        [
            "-n",
            "_strip_tool_call_echo|_last_clean_assistant_text",
            "src/monkeybot",
        ]
    )


def test_no_tool_calls_substring_in_provider_or_core() -> None:
    _rg_no_matches(
        [
            "-n",
            "tool_calls\"",
            str(_REPO_ROOT / "src/monkeybot/core"),
            str(_REPO_ROOT / "src/monkeybot/providers"),
        ]
    )
