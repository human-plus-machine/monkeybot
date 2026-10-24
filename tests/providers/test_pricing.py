"""Tests for :mod:`monkeybot.providers.pricing`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import monkeybot.providers.pricing as pricing
from monkeybot.core.config import ConfigError, validate_monkeybot_yaml_doc
from monkeybot.providers.pricing import (
    estimate_cost,
    parse_pricing_section,
    pricing_for,
    register_model_pricing,
    unregister_model_pricing,
)

DEEPSEEK = "deepseek-ai/deepseek-v4.1-flash-maas"


@pytest.fixture(autouse=True)
def _isolated_pricing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    monkeypatch.delenv("MONKEYBOT_AGENT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pricing, "_registered_pricing", {})
    monkeypatch.setattr(pricing, "_warned_unknown_models", set())
    monkeypatch.setattr(pricing, "_config_pricing_cache", None)
    yield


def _write_config(path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_CONFIG", str(path))
    return path


def test_pricing_for_exact_match() -> None:
    assert pricing_for("gpt-5") == (1.25, 10.00, 0.00, 0.125)


def test_pricing_for_longest_prefix_match() -> None:
    result = pricing_for("claude-haiku-4-5@20251101")
    assert result == pricing_for("claude-haiku-4-5")
    assert result == (1.00, 5.00, 1.25, 0.10)


def test_pricing_for_unknown_returns_zeros(caplog: pytest.LogCaptureFixture) -> None:
    assert "gpt-5" in pricing.MODEL_PRICING
    with caplog.at_level("WARNING", logger="monkeybot.providers.pricing"):
        assert pricing_for("nonexistent-model") == (0.0, 0.0, 0.0, 0.0)
    assert "unknown model for pricing lookup: nonexistent-model" in caplog.text


def test_pricing_for_bedrock_converse_models() -> None:
    assert pricing_for("us.xai.grok-4.6") == pricing_for("grok-4")
    assert pricing_for("us.amazon.nova-pro-v1:0") == pricing_for("nova-pro")
    assert pricing_for("us.meta.llama3-3-70b-instruct-v1:0") == pricing_for("llama3-3-70b-instruct")
    assert pricing_for("us.xai.grok-4.6")[0] > 0.0
    assert pricing_for("jp.anthropic.claude-sonnet-4-6")[0] > 0.0


def test_estimate_cost_input_output_only() -> None:
    assert estimate_cost("gpt-5", 1_000_000, 1_000_000) == pytest.approx(11.25)


def test_estimate_cost_with_cache_tokens() -> None:
    cost = estimate_cost(
        "claude-sonnet-4",
        0,
        0,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    # short (default): 3.75 write + 0.30 read
    assert cost == pytest.approx(4.05)


def test_estimate_cost_long_retention_uses_2x_anthropic_write() -> None:
    short = estimate_cost(
        "claude-sonnet-4",
        0,
        0,
        cache_creation_tokens=1_000_000,
        cache_retention="short",
    )
    long = estimate_cost(
        "claude-sonnet-4",
        0,
        0,
        cache_creation_tokens=1_000_000,
        cache_retention="long",
    )
    assert short == pytest.approx(3.75)
    assert long == pytest.approx(6.00)
    # Under-report if long were priced as short: (2-1.25)/2 = 37.5%
    assert (long - short) / long == pytest.approx(0.375)


def test_estimate_cost_long_retention_noop_when_no_write_rate() -> None:
    # OpenAI has cache_write=0; retention must not invent an Anthropic premium.
    short = estimate_cost(
        "gpt-5",
        0,
        0,
        cache_read_tokens=1_000_000,
        cache_retention="short",
    )
    long = estimate_cost(
        "gpt-5",
        0,
        0,
        cache_read_tokens=1_000_000,
        cache_retention="long",
    )
    assert short == long == pytest.approx(0.125)


def test_estimate_cost_unknown_model_zero() -> None:
    assert pricing.MODEL_PRICING.get("gpt-5") is not None
    assert estimate_cost("nope", 1000, 1000, cache_read_tokens=1000) == 0.0


def test_estimate_cost_zero_tokens() -> None:
    assert pricing.MODEL_PRICING.get("gpt-5") is not None
    assert estimate_cost("gpt-5", 0, 0) == 0.0


def test_utils_reexports_estimate_cost() -> None:
    from monkeybot.providers._utils import estimate_cost as ec

    assert ec is pricing.estimate_cost


def test_unknown_model_warns_once_per_process(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="monkeybot.providers.pricing"):
        for _ in range(5):
            estimate_cost("mystery-model", 100, 100)
    assert caplog.text.count("unknown model for pricing lookup: mystery-model") == 1


def test_yaml_pricing_prices_full_id_and_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_config(
        tmp_path / "monkeybot.yaml",
        "pricing:\n"
        "  deepseek-v4.1-flash:\n"
        "    input: 0.30\n"
        "    output: 1.20\n"
        "    cache_read: 0.03\n"
        f"    aliases: [{DEEPSEEK}, deepseek-v4.1-flash-maas]\n",
        monkeypatch,
    )
    with caplog.at_level("WARNING", logger="monkeybot.providers.pricing"):
        assert pricing_for(DEEPSEEK) == (0.30, 1.20, 0.0, 0.03)
        assert pricing_for("deepseek-v4.1-flash-maas") == (0.30, 1.20, 0.0, 0.03)
    assert "unknown model" not in caplog.text
    assert estimate_cost(DEEPSEEK, 1_000_000, 1_000_000, cache_read_tokens=1_000_000) == (
        pytest.approx(1.53)
    )


def test_yaml_pricing_reloads_when_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(
        tmp_path / "monkeybot.yaml",
        "pricing:\n  my-model:\n    input: 1\n    output: 2\n",
        monkeypatch,
    )
    assert pricing_for("my-model") == (1.0, 2.0, 0.0, 0.0)
    cfg.write_text("pricing:\n  my-model:\n    input: 3\n    output: 40\n", encoding="utf-8")
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert pricing_for("my-model") == (3.0, 40.0, 0.0, 0.0)


def test_yaml_pricing_overrides_builtin_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(
        tmp_path / "monkeybot.yaml",
        "pricing:\n  gpt-5:\n    input: 9\n    output: 9\n",
        monkeypatch,
    )
    assert pricing_for("gpt-5") == (9.0, 9.0, 0.0, 0.0)
    assert pricing_for("claude-haiku-4-5") == (1.00, 5.00, 1.25, 0.10)


def test_invalid_yaml_pricing_is_ignored_at_turn_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_config(
        tmp_path / "monkeybot.yaml",
        "pricing:\n  broken:\n    input: cheap\n    output: 1\n",
        monkeypatch,
    )
    with caplog.at_level("WARNING", logger="monkeybot.providers.pricing"):
        assert pricing_for("gpt-5") == (1.25, 10.00, 0.00, 0.125)
        assert pricing_for("gpt-5") == (1.25, 10.00, 0.00, 0.125)
    assert caplog.text.count("ignoring pricing in") == 1


def test_register_model_pricing_wins_over_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_config(
        tmp_path / "monkeybot.yaml",
        f"pricing:\n  {DEEPSEEK}:\n    input: 5\n    output: 5\n",
        monkeypatch,
    )
    register_model_pricing(DEEPSEEK, input=0.30, output=1.20, cache_read=0.03)
    with caplog.at_level("WARNING", logger="monkeybot.providers.pricing"):
        assert pricing_for(DEEPSEEK) == (0.30, 1.20, 0.0, 0.03)
    assert "unknown model" not in caplog.text
    unregister_model_pricing(DEEPSEEK)
    assert pricing_for(DEEPSEEK) == (5.0, 5.0, 0.0, 0.0)


def test_register_model_pricing_aliases_and_prefix_match() -> None:
    register_model_pricing("acme-chat", input=1, output=2, aliases=["vendor/acme-chat-latest"])
    assert pricing_for("vendor/acme-chat-latest") == (1.0, 2.0, 0.0, 0.0)
    assert pricing_for("acme-chat@2026-10-01") == (1.0, 2.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"input": -1, "output": 1}, "non-negative"),
        ({"input": float("nan"), "output": 1}, "non-negative"),
        ({"input": True, "output": 1}, "must be a number"),
    ],
)
def test_register_model_pricing_rejects_bad_rates(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        register_model_pricing("bad-model", **kwargs)


@pytest.mark.parametrize(
    ("section", "message"),
    [
        (["gpt-5"], "must be a mapping"),
        ({"m": {"output": 1}}, "missing input"),
        ({"m": {"input": 1, "output": 1, "cache_hit": 0.1}}, "unknown field"),
        ({"m": {"input": 1, "output": 1, "aliases": "other"}}, "aliases must be a list"),
        ({"m": 0.3}, "mapping of rates"),
    ],
)
def test_parse_pricing_section_errors(section: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_pricing_section(section)


def test_validate_monkeybot_yaml_rejects_bad_pricing() -> None:
    validate_monkeybot_yaml_doc(
        {"model": {"provider": "gemini"}, "pricing": {"m": {"input": 1, "output": 2}}}
    )
    with pytest.raises(ConfigError, match=r"pricing\.m\.output"):
        validate_monkeybot_yaml_doc(
            {"model": {"provider": "gemini"}, "pricing": {"m": {"input": 1, "output": -2}}}
        )
