"""Tests for phase 5.3 outbound-to-provider secret/canary scanning."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from monkeybot.core.context.secret_egress import (
    Hit,
    ScanUnit,
    SecretScanner,
    extract_scan_units,
    redact_units,
    scan_and_redact,
)
from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)


def _publish_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(
        "monkeybot.core.context.secret_egress._BRIDGE_URL_FILE", cdp_file
    )


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


# --- extract_scan_units / redact_units (pure) ------------------------------


def test_extract_scan_units_skips_plain_user_text() -> None:
    """A real user-authored Text block is never a scan unit."""
    messages = [Message(role="user", content=[Text(text="my password is hunter2")])]
    assert extract_scan_units(messages) == []


def test_extract_scan_units_finds_assistant_text() -> None:
    messages = [Message(role="assistant", content=[Text(text="the secret is X")])]
    units = extract_scan_units(messages)
    assert units == [ScanUnit(message_index=0, block_path=(0,), text="the secret is X")]


def test_extract_scan_units_finds_tool_response_text_not_sibling_user_text() -> None:
    """A user-role message can carry a ToolResponse; only that block is a unit."""
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(id="c1", tool_name="run_command", result=[Text(text="leaked-value")]),
            ],
        )
    ]
    units = extract_scan_units(messages)
    assert units == [ScanUnit(message_index=0, block_path=(0, 0), text="leaked-value")]


def test_extract_scan_units_ignores_empty_tool_request_args() -> None:
    messages = [
        Message(role="assistant", content=[ToolRequest(id="c1", name="run_command", args={})]),
    ]
    assert extract_scan_units(messages) == []


def test_extract_scan_units_finds_tool_request_args() -> None:
    """A blocked tool call's ToolRequest still carries the secret in `args` —
    it must be scanned too, not just the assistant text around it."""
    messages = [
        Message(
            role="assistant",
            content=[
                ToolRequest(id="c1", name="run_command", args={"command": "echo the-secret"}),
            ],
        )
    ]
    units = extract_scan_units(messages)
    assert units == [
        ScanUnit(
            message_index=0,
            block_path=(0,),
            text=json.dumps({"command": "echo the-secret"}, ensure_ascii=False),
        )
    ]


def test_extract_scan_units_finds_thinking() -> None:
    messages = [
        Message(
            role="assistant",
            content=[Thinking(thinking="the secret is X", signature="sig")],
        )
    ]
    units = extract_scan_units(messages)
    assert units == [ScanUnit(message_index=0, block_path=(0,), text="the secret is X")]


def test_extract_scan_units_skips_redacted_thinking() -> None:
    messages = [
        Message(
            role="assistant",
            content=[RedactedThinking(data="opaque-blob")],
        )
    ]
    assert extract_scan_units(messages) == []


def test_redact_units_replaces_assistant_text() -> None:
    messages = [Message(role="assistant", content=[Text(text="the secret is X")])]
    units = extract_scan_units(messages)
    redacted = redact_units(messages, units, {0})
    block = redacted[0].content[0]
    assert isinstance(block, Text)
    assert block.text == "[withheld: credential detected]"


def test_redact_units_replaces_only_tool_response_result_not_other_content() -> None:
    messages = [
        Message(
            role="user",
            content=[
                Text(text="real user text stays"),
                ToolResponse(id="c1", tool_name="run_command", result=[Text(text="leaked")]),
            ],
        )
    ]
    units = extract_scan_units(messages)
    redacted = redact_units(messages, units, {0})
    user_text_block = redacted[0].content[0]
    tool_response = redacted[0].content[1]
    assert isinstance(user_text_block, Text)
    assert user_text_block.text == "real user text stays"
    assert isinstance(tool_response, ToolResponse)
    inner = tool_response.result[0]
    assert isinstance(inner, Text)
    assert inner.text == "[withheld: credential detected]"


def test_redact_units_redacts_tool_request_args_keeping_id_and_name() -> None:
    """`id`/`name` survive redaction so a sibling ToolResponse still correlates."""
    messages = [
        Message(
            role="assistant",
            content=[
                ToolRequest(id="c1", name="run_command", args={"command": "echo the-secret"}),
            ],
        )
    ]
    units = extract_scan_units(messages)
    redacted = redact_units(messages, units, {0})
    block = redacted[0].content[0]
    assert isinstance(block, ToolRequest)
    assert block.id == "c1"
    assert block.name == "run_command"
    assert block.args == {"redacted": "[withheld: credential detected]"}


def test_redact_units_swaps_thinking_for_redacted_thinking() -> None:
    """Replacing thinking text in place would break Anthropic's signature;
    RedactedThinking is the wire type that path already knows how to send."""
    messages = [
        Message(
            role="assistant",
            content=[
                Thinking(thinking="the secret is X", signature="sig-must-not-reuse"),
                Text(text="ok"),
            ],
        )
    ]
    units = extract_scan_units(messages)
    redacted = redact_units(messages, units, {0})
    thinking = redacted[0].content[0]
    text = redacted[0].content[1]
    assert isinstance(thinking, RedactedThinking)
    assert thinking.data == "[withheld: credential detected]"
    assert isinstance(text, Text)
    assert text.text == "[withheld: credential detected]"


def test_redact_units_leaves_unflagged_messages_untouched() -> None:
    messages = [
        Message(role="assistant", content=[Text(text="clean")]),
        Message(role="assistant", content=[Text(text="dirty")]),
    ]
    units = extract_scan_units(messages)
    redacted = redact_units(messages, units, {1})
    assert redacted[0] is messages[0]
    block = redacted[1].content[0]
    assert isinstance(block, Text)
    assert block.text == "[withheld: credential detected]"


def test_redact_units_no_hits_returns_messages_list_copy() -> None:
    messages = [Message(role="assistant", content=[Text(text="clean")])]
    assert redact_units(messages, extract_scan_units(messages), set()) == messages


# --- scan_and_redact (pure, given a fake scanner) --------------------------


class _FakeScanner:
    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits

    def scan(self, texts: list[str]) -> list[Hit]:
        return self._hits


def test_scan_and_redact_no_units_short_circuits() -> None:
    messages = [Message(role="user", content=[Text(text="hello")])]
    outcome = scan_and_redact(messages, scanner=_FakeScanner([Hit(index=0, kind="secret")]))
    assert outcome.hits == []
    assert outcome.messages == messages


def test_scan_and_redact_redacts_hit_message() -> None:
    messages = [Message(role="assistant", content=[Text(text="the secret is X")])]
    outcome = scan_and_redact(messages, scanner=_FakeScanner([Hit(index=0, kind="secret")]))
    assert outcome.hit_message_indices == frozenset({0})
    block = outcome.messages[0].content[0]
    assert isinstance(block, Text)
    assert block.text == "[withheld: credential detected]"


def test_scan_and_redact_no_hits_returns_original() -> None:
    messages = [Message(role="assistant", content=[Text(text="clean")])]
    outcome = scan_and_redact(messages, scanner=_FakeScanner([]))
    assert outcome.hits == []
    assert outcome.hit_message_indices == frozenset()


# --- SecretScanner (HTTP mocked, mirrors login.py's test style) ------------


def test_scanner_no_op_without_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "monkeybot.core.context.secret_egress._BRIDGE_URL_FILE", tmp_path / "missing"
    )
    scanner = SecretScanner()
    assert scanner.scan(["anything"]) == []


def test_scanner_posts_bearer_and_maps_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish_bridge(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        captured["req"] = req
        return _FakeResponse({"hits": [{"index": 1, "kind": "secret"}]})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)

    scanner = SecretScanner()
    hits = scanner.scan(["clean", "dirty", "also clean"])

    assert hits == [Hit(index=1, kind="secret")]
    req = captured["req"]
    assert isinstance(req, Request)
    assert req.full_url == "http://127.0.0.1:9333/json/scan"
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(req.data.decode("utf-8")) == {"texts": ["clean", "dirty", "also clean"]}


def test_scanner_caches_clean_texts_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_bridge(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        call_count["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        # Only ever asked about "dirty" after the first call.
        assert body["texts"] == (["clean", "dirty"] if call_count["n"] == 1 else ["dirty"])
        return _FakeResponse({"hits": [{"index": body["texts"].index("dirty"), "kind": "secret"}]})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)

    scanner = SecretScanner()
    scanner.scan(["clean", "dirty"])
    scanner.scan(["clean", "dirty"])
    assert call_count["n"] == 2


def test_scanner_clean_cache_expires_after_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest cached clean is re-scanned once the TTL elapses — e.g. a
    password saved mid-session must eventually be caught even if its exact
    text was scanned clean earlier in the same process lifetime."""
    _publish_bridge(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        call_count["n"] += 1
        return _FakeResponse({"hits": []})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)

    clock = {"t": 0.0}
    scanner = SecretScanner(clock=lambda: clock["t"])

    scanner.scan(["clean"])
    assert call_count["n"] == 1

    clock["t"] += 1.0  # well within the TTL
    scanner.scan(["clean"])
    assert call_count["n"] == 1

    from monkeybot.core.context.secret_egress import _CLEAN_CACHE_TTL_S

    clock["t"] += _CLEAN_CACHE_TTL_S
    scanner.scan(["clean"])
    assert call_count["n"] == 2


def test_scanner_does_not_cache_a_failed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_bridge(tmp_path, monkeypatch)

    def fail_open(req: Request, timeout: object = None) -> None:
        raise HTTPError(req.full_url, 500, "boom", hdrs=None, fp=BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fail_open)

    scanner = SecretScanner()
    assert scanner.scan(["maybe-secret"]) == []

    # Not cached as clean after the failure, so a later working request
    # re-checks it rather than skipping it as already-confirmed-safe.
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        captured["texts"] = json.loads(req.data.decode("utf-8"))["texts"]
        return _FakeResponse({"hits": []})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)
    scanner.scan(["maybe-secret"])
    assert captured["texts"] == ["maybe-secret"]


def test_scanner_batches_by_json_body_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_bridge(tmp_path, monkeypatch)
    one_item = len(json.dumps({"texts": ["aaaaaa"]}).encode("utf-8"))
    monkeypatch.setattr("monkeybot.core.context.secret_egress._SCAN_MAX_BATCH_BYTES", one_item)
    calls: list[list[str]] = []

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        texts = json.loads(req.data.decode("utf-8"))["texts"]
        calls.append(texts)
        return _FakeResponse({"hits": []})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)

    scanner = SecretScanner()
    # One item's JSON body equals the cap; two items exceed it — so this
    # still forces one item per batch, without tripping the oversized path.
    scanner.scan(["aaaaaa", "bbbbbb", "cccccc"])
    assert len(calls) == 3
    assert [c[0] for c in calls] == ["aaaaaa", "bbbbbb", "cccccc"]


def test_scanner_fails_closed_on_text_too_large_to_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text whose JSON body does not fit even its own batch is redacted
    without a round trip, rather than silently passed through unscanned."""
    _publish_bridge(tmp_path, monkeypatch)
    short_body = len(json.dumps({"texts": ["short"]}).encode("utf-8"))
    monkeypatch.setattr("monkeybot.core.context.secret_egress._SCAN_MAX_BATCH_BYTES", short_body)
    calls: list[list[str]] = []

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        texts = json.loads(req.data.decode("utf-8"))["texts"]
        calls.append(texts)
        return _FakeResponse({"hits": []})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)

    scanner = SecretScanner()
    hits = scanner.scan(["short", "this-text-is-way-over-the-cap"])

    assert hits == [Hit(index=1, kind="secret")]
    assert calls == [["short"]]

    hits_again = scanner.scan(["this-text-is-way-over-the-cap"])
    assert hits_again == [Hit(index=0, kind="secret")]


def test_scanner_fails_closed_when_json_escaping_exceeds_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw UTF-8 can fit the cap while the JSON-escaped body does not
    (quotes become \\\"). Fail closed without a round trip."""
    _publish_bridge(tmp_path, monkeypatch)
    quoted = '"' * 20
    assert len(quoted.encode("utf-8")) == 20
    json_body = len(json.dumps({"texts": [quoted]}).encode("utf-8"))
    assert json_body > 20
    monkeypatch.setattr("monkeybot.core.context.secret_egress._SCAN_MAX_BATCH_BYTES", 30)
    calls: list[list[str]] = []

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        calls.append(json.loads(req.data.decode("utf-8"))["texts"])
        return _FakeResponse({"hits": []})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)

    scanner = SecretScanner()
    hits = scanner.scan([quoted])
    assert hits == [Hit(index=0, kind="secret")]
    assert calls == []


def test_scanner_fails_closed_on_http_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 413 from the bridge (body over the cap despite chunking) redacts
    the batch instead of treating the error as 'unresolved' / fail-open."""
    _publish_bridge(tmp_path, monkeypatch)

    def fail_413(req: Request, timeout: object = None) -> None:
        raise HTTPError(req.full_url, 413, "payload too large", hdrs=None, fp=BytesIO(b""))  # type: ignore[arg-type]

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fail_413)

    scanner = SecretScanner()
    assert scanner.scan(["maybe-secret"]) == [Hit(index=0, kind="secret")]

    # Not cached as clean — a later working request re-checks it.
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeResponse:
        captured["texts"] = json.loads(req.data.decode("utf-8"))["texts"]
        return _FakeResponse({"hits": []})

    monkeypatch.setattr("monkeybot.core.context.secret_egress._LOOPBACK_OPENER.open", fake_open)
    assert scanner.scan(["maybe-secret"]) == []
    assert captured["texts"] == ["maybe-secret"]
