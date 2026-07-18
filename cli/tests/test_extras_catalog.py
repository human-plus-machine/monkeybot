"""Tests for extras catalog + ``monkeybot new --with`` parsing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from monkeybot_cli.commands.new import _resolve_extras_from_args
from monkeybot_cli.compat import COMPATIBLE_CORE_RANGE
from monkeybot_cli.extras_catalog import normalize_extra_token, provider_extra_name

CLI_ROOT = Path(__file__).resolve().parents[1]


def test_normalize_extra_token_features_and_providers() -> None:
    assert normalize_extra_token("postgres") == "postgres"
    assert normalize_extra_token("web-search") == "web-search"
    assert normalize_extra_token("web_search") == "web-search"
    assert normalize_extra_token("anthropic") == "claude"
    assert normalize_extra_token("aws_bedrock") == "bedrock"
    assert normalize_extra_token("claude") == "claude"
    assert normalize_extra_token("fake") is None
    assert normalize_extra_token("not-a-real-extra") is None


def test_provider_extra_name() -> None:
    assert provider_extra_name("openai") == "openai"
    assert provider_extra_name("anthropic") == "claude"
    assert provider_extra_name("fake") is None


def test_fake_not_in_new_provider_menu() -> None:
    from monkeybot_cli.extras_catalog import PROVIDER_CHOICES

    assert all(c.key != "fake" for c in PROVIDER_CHOICES)


def test_resolve_extras_from_args_comma_and_repeat() -> None:
    extras, unknown = _resolve_extras_from_args(["postgres,sandbox", "observability", "claude"])
    assert extras == ["postgres", "sandbox", "observability", "claude"]
    assert unknown == []


def test_resolve_extras_unknown() -> None:
    extras, unknown = _resolve_extras_from_args(["postgres", "nope"])
    assert extras == ["postgres"]
    assert unknown == ["nope"]


def test_fake_credentials_optional() -> None:
    from monkeybot_cli.providers import credentials_present, spec_for_provider

    spec = spec_for_provider("fake")
    assert spec is not None
    assert spec.credentials_optional is True
    assert credentials_present(spec) is True


def test_new_with_extras_cli(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "monkeybot_cli.main",
            "new",
            "--dest",
            str(tmp_path),
            "--yes",
            "--provider",
            "openai",
            "--with",
            "postgres,sandbox",
            "--with",
            "bedrock",
        ],
        cwd=CLI_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(CLI_ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        f'"monkeybot[openai,sandbox,web-search,postgres,bedrock]{COMPATIBLE_CORE_RANGE}"'
        in text
    )
