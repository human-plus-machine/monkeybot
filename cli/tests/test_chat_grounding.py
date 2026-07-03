"""Tests for CLI rendering of native web-search grounding events."""

from __future__ import annotations

from monkeybot.core.runtime.events import GroundingEvent

from monkeybot_cli.commands.chat import _hyperlink, _print_grounding


def test_hyperlink_non_tty_falls_back_to_plain_url(capsys) -> None:  # type: ignore[no-untyped-def]
    assert _hyperlink("https://example.com", "Example") == "Example (https://example.com)"


def test_hyperlink_non_tty_omits_duplicate_label() -> None:
    assert _hyperlink("https://example.com", "https://example.com") == "https://example.com"


def test_hyperlink_empty_url_returns_label() -> None:
    assert _hyperlink("", "Example") == "Example"


def test_print_grounding_shows_queries_and_links(capsys) -> None:  # type: ignore[no-untyped-def]
    evt = GroundingEvent(
        request_id="r",
        sources=[
            {"title": "Example Site", "uri": "https://example.com"},
            {"title": "", "uri": "https://example.org"},
        ],
        search_queries=["best pizza nyc"],
    )
    _print_grounding(evt)
    out = capsys.readouterr().out
    assert "grounded search" in out
    assert "best pizza nyc" in out
    assert "Example Site (https://example.com)" in out
    assert "https://example.org" in out


def test_print_grounding_truncates_many_sources(capsys) -> None:  # type: ignore[no-untyped-def]
    evt = GroundingEvent(
        request_id="r",
        sources=[{"title": f"Source {i}", "uri": f"https://example.com/{i}"} for i in range(8)],
        search_queries=["query"],
    )
    _print_grounding(evt)
    out = capsys.readouterr().out
    assert "and 3 more" in out


def test_print_grounding_no_op_when_empty(capsys) -> None:  # type: ignore[no-untyped-def]
    evt = GroundingEvent(request_id="r", sources=[], search_queries=[])
    _print_grounding(evt)
    out = capsys.readouterr().out
    assert out == ""
