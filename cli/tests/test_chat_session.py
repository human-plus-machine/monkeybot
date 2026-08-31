"""Unit tests for ChatSessionController."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx

from monkeybot_cli.chat_session import (
    ChatSessionController,
    ChatUiEvent,
    HitlAnswer,
    HitlRequest,
    SseFrame,
    iter_sse_frames,
)


def test_post_hitl_failure_emits_hitl_failed() -> None:
    events: list[ChatUiEvent] = []

    async def _run() -> None:
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
        )
        request = httpx.Request("POST", "http://localhost:8080/x")
        response = httpx.Response(500, request=request)

        async def fake_post(url: str, json: object) -> httpx.Response:
            raise httpx.HTTPStatusError("boom", request=request, response=response)

        controller._client = AsyncMock()
        controller._client.post = fake_post
        controller.session_id = "sess"

        ok = await controller._post_hitl(
            "http://localhost:8080/x",
            {"approved": False},
            label="Tool confirmation",
        )
        assert ok is False

    asyncio.run(_run())
    assert any(e.kind == "hitl_failed" for e in events)
    assert "Tool confirmation failed" in events[-1].payload["message"]


def test_context_usage_event_emits_usage_updated() -> None:
    from monkeybot.core.runtime.events import ContextUsage
    from monkeybot_cli.chat_session import _TurnState

    events: list[ChatUiEvent] = []

    async def _run() -> None:
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
        )
        state = _TurnState()
        await controller._dispatch_turn_event(
            ContextUsage(
                request_id="rid-1",
                estimated_tokens=12_500,
                context_window_tokens=200_000,
            ),
            "rid-1",
            state,
        )

    asyncio.run(_run())
    updated = [e for e in events if e.kind == "usage_updated"]
    assert len(updated) == 1
    usage = updated[0].payload["usage"]
    assert usage is not None
    assert usage.estimated_prompt_tokens == 12_500
    assert usage.context_window_tokens == 200_000


def test_context_summarized_does_not_fetch_usage() -> None:
    """Persisted /usage mid-turn would clobber the live post-compaction ring."""
    from monkeybot.core.runtime.events import ContextSummarized
    from monkeybot_cli.chat_session import _TurnState

    events: list[ChatUiEvent] = []

    async def _run() -> None:
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
        )
        controller._fetch_usage = AsyncMock()  # type: ignore[method-assign]
        state = _TurnState()
        await controller._dispatch_turn_event(
            ContextSummarized(request_id="rid-1", turns_summarized=4),
            "rid-1",
            state,
        )
        controller._fetch_usage.assert_not_awaited()

    asyncio.run(_run())
    assert any(e.kind == "summarized" and e.payload.get("turns") == 4 for e in events)


def test_hitl_reader_confirm_yes() -> None:
    events: list[ChatUiEvent] = []

    async def reader(req: HitlRequest) -> HitlAnswer:
        assert req.kind == "confirm"
        return HitlAnswer(approved=True, text="y")

    async def _run() -> HitlAnswer:
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
            hitl_reader=reader,
        )
        return await controller._await_hitl(
            HitlRequest(kind="confirm", prompt="Approve?", tool_call_id="c1")
        )

    answer = asyncio.run(_run())
    assert answer.approved is True
    assert any(e.kind == "hitl_required" for e in events)


def test_provide_hitl_answer_resolves_future() -> None:
    async def _run() -> HitlAnswer:
        controller = ChatSessionController(base="http://localhost:8080")
        task = asyncio.create_task(
            controller._await_hitl(HitlRequest(kind="elicit", prompt="Input", elicitation_id="e1"))
        )
        await asyncio.sleep(0)
        controller.provide_hitl_answer(HitlAnswer(text="hello"))
        return await task

    assert asyncio.run(_run()).text == "hello"


def test_await_hitl_emit_includes_timeout_and_schema(monkeypatch: object) -> None:
    events: list[ChatUiEvent] = []

    async def reader(req: HitlRequest) -> HitlAnswer:
        assert req.schema is not None
        assert "name" in (req.schema.get("properties") or {})
        return HitlAnswer(text="ok")

    async def _run() -> None:
        monkeypatch.setenv("PENDING_RESPONSE_TIMEOUT_SEC", "42")  # type: ignore[attr-defined]
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
            hitl_reader=reader,
        )
        await controller._await_hitl(
            HitlRequest(
                kind="elicit",
                prompt="What is your name?",
                elicitation_id="e1",
                schema={
                    "properties": {
                        "name": {"type": "string", "description": "Full name"},
                    }
                },
                timeout_sec=42.0,
            )
        )

    asyncio.run(_run())
    hitl = next(e for e in events if e.kind == "hitl_required")
    assert hitl.payload["prompt"] == "What is your name?"
    assert hitl.payload["timeout_sec"] == 42.0
    assert hitl.payload["schema"]["properties"]["name"]["type"] == "string"
    assert hitl.payload["hitl_kind"] == "elicit"


def test_handle_elicit_uses_payload_message(monkeypatch: object) -> None:
    from monkeybot.core.runtime.events import ActionRequiredEvent

    events: list[ChatUiEvent] = []

    async def reader(req: HitlRequest) -> HitlAnswer:
        assert req.prompt == "Pick a timezone"
        assert req.schema is not None
        return HitlAnswer(text='{"tz":"UTC"}')

    async def _run() -> None:
        monkeypatch.setenv("PENDING_RESPONSE_TIMEOUT_SEC", "120")  # type: ignore[attr-defined]
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
            hitl_reader=reader,
        )
        controller.session_id = "sess"
        posted: list[dict] = []

        async def fake_post(url: str, json: object) -> httpx.Response:
            posted.append(dict(json))  # type: ignore[arg-type]
            request = httpx.Request("POST", url)
            return httpx.Response(202, json={"ok": True}, request=request)

        controller._client = AsyncMock()
        controller._client.post = fake_post
        await controller._handle_elicit(
            ActionRequiredEvent(
                id="el-1",
                payload={
                    "message": "Pick a timezone",
                    "requestedSchema": {
                        "properties": {"tz": {"type": "string", "description": "IANA tz"}},
                    },
                },
            )
        )
        assert posted and posted[0]["user_data"] == {"tz": "UTC"}

    asyncio.run(_run())
    hitl = next(e for e in events if e.kind == "hitl_required")
    assert hitl.payload["prompt"] == "Pick a timezone"
    assert "tz" in hitl.payload["schema"]["properties"]


def test_handle_tool_confirm_emits_arguments(monkeypatch: object) -> None:
    from monkeybot.core.runtime.events import ToolConfirmationRequestEvent

    events: list[ChatUiEvent] = []

    async def reader(req: HitlRequest) -> HitlAnswer:
        assert req.tool_name == "run_command"
        assert req.arguments == {"command": "rm -rf /"}
        return HitlAnswer(approved=True, text="y")

    async def _run() -> None:
        monkeypatch.setenv("PENDING_RESPONSE_TIMEOUT_SEC", "99")  # type: ignore[attr-defined]
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
            hitl_reader=reader,
        )
        controller.session_id = "sess"

        async def fake_post(url: str, json: object) -> httpx.Response:
            request = httpx.Request("POST", url)
            return httpx.Response(202, json={"ok": True}, request=request)

        controller._client = AsyncMock()
        controller._client.post = fake_post
        await controller._handle_tool_confirm(
            ToolConfirmationRequestEvent(
                tool_call_id="c1",
                tool_name="run_command",
                arguments={"command": "rm -rf /"},
                prompt="Delete everything?",
            )
        )

    asyncio.run(_run())
    hitl = next(e for e in events if e.kind == "hitl_required")
    assert hitl.payload["hitl_kind"] == "confirm"
    assert hitl.payload["tool_name"] == "run_command"
    assert hitl.payload["arguments"]["command"] == "rm -rf /"
    assert hitl.payload["timeout_sec"] == 99.0
    assert "[y/N]" not in hitl.payload["prompt"]


def test_format_schema_field_lines() -> None:
    from monkeybot_cli.chat_session import format_schema_field_lines

    lines = format_schema_field_lines(
        {
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "count": {"type": "integer"},
            }
        }
    )
    assert any("city (string) — City name" in line for line in lines)
    assert any("count (integer)" in line for line in lines)


def test_iter_sse_frames_tracks_event_id() -> None:
    async def _run() -> list[SseFrame]:
        class FakeResp:
            async def aiter_lines(self):
                yield "id: 7"
                yield 'data: {"type":"Ping"}'
                yield ""
                yield "id: 8"
                yield 'data: {"ok":true}'
                yield ""

        return [frame async for frame in iter_sse_frames(FakeResp())]  # type: ignore[arg-type]

    frames = asyncio.run(_run())
    assert frames == [
        SseFrame(data='{"type":"Ping"}', event_id=7),
        SseFrame(data='{"ok":true}', event_id=8),
    ]


def test_abort_turn_posts_cancel() -> None:
    async def _run() -> None:
        events: list[ChatUiEvent] = []
        controller = ChatSessionController(base="http://localhost:8080", emit=events.append)
        controller.session_id = "sess-1"
        controller._active_request_id = "req-1"
        posted: list[tuple[str, dict]] = []

        async def fake_post(url: str, json: object) -> httpx.Response:
            posted.append((url, dict(json)))  # type: ignore[arg-type]
            request = httpx.Request("POST", url)
            return httpx.Response(200, request=request)

        controller._client = AsyncMock()
        controller._client.post = fake_post

        controller.abort_turn()
        assert controller._turn_abort.is_set()
        await asyncio.sleep(0.05)
        assert posted
        assert posted[0][0].endswith("/sessions/sess-1/cancel")
        assert posted[0][1] == {"request_id": "req-1"}
        assert controller._last_cancel_ok is True

    asyncio.run(_run())


def test_attach_or_create_session_live() -> None:
    async def _run() -> None:
        controller = ChatSessionController(base="http://localhost:8080")
        controller._client = AsyncMock()

        async def fake_get(url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(200, json={"session_id": "abc"}, request=request)

        controller._client.get = fake_get
        await controller._attach_or_create_session("abc")
        assert controller.session_id == "abc"
        controller._client.post.assert_not_called()

    asyncio.run(_run())


def test_attach_or_create_session_recreates_on_404() -> None:
    async def _run() -> None:
        controller = ChatSessionController(base="http://localhost:8080")
        controller._client = AsyncMock()

        async def fake_get(url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(404, request=request)

        async def fake_post(url: str, json: object) -> httpx.Response:
            request = httpx.Request("POST", url)
            return httpx.Response(
                201, json={"session_id": "abc", "created_at": 1}, request=request
            )

        controller._client.get = fake_get
        controller._client.post = fake_post
        await controller._attach_or_create_session("abc")
        assert controller.session_id == "abc"

    asyncio.run(_run())


def test_emit_transcript_backfill() -> None:
    async def _run() -> None:
        events: list[ChatUiEvent] = []
        controller = ChatSessionController(base="http://localhost:8080", emit=events.append)
        controller.session_id = "sess"
        controller._client = AsyncMock()

        async def fake_get(url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                json={
                    "session_id": "sess",
                    "messages": [
                        {"role": "user", "text": "hi"},
                        {"role": "assistant", "text": "hello"},
                    ],
                },
                request=request,
            )

        controller._client.get = fake_get
        await controller._emit_transcript_backfill()
        assert any(e.kind == "transcript_backfill" for e in events)
        msgs = next(e for e in events if e.kind == "transcript_backfill").payload["messages"]
        assert msgs[0]["role"] == "user"
        assert msgs[1]["text"] == "hello"

    asyncio.run(_run())


def test_turn_aborted_includes_cancel_ok() -> None:
    async def _run() -> None:
        events: list[ChatUiEvent] = []
        controller = ChatSessionController(base="http://localhost:8080", emit=events.append)
        controller.session_id = "s"
        controller._client = AsyncMock()
        controller._active_request_id = "r1"
        controller._last_cancel_ok = True
        controller._turn_abort.set()

        async def empty_dequeue() -> None:
            return None

        controller._dequeue_payload = empty_dequeue  # type: ignore[method-assign]
        await controller._run_turn("r1")
        aborted = [e for e in events if e.kind == "turn_aborted"]
        assert aborted
        assert aborted[0].payload.get("cancel_ok") is True

    asyncio.run(_run())


def test_close_deletes_session_and_captures_report_dir() -> None:
    async def _run() -> None:
        controller = ChatSessionController(base="http://localhost:8080")
        controller.session_id = "sess-close"
        client = AsyncMock()
        request = httpx.Request("DELETE", "http://localhost:8080/sessions/sess-close")
        client.delete = AsyncMock(
            return_value=httpx.Response(
                200,
                json={
                    "deleted": True,
                    "transcript_dir": "/tmp/ws/.monkeybot/transcripts/20260714T150000Z_sess-close",
                },
                request=request,
            )
        )
        client.aclose = AsyncMock()
        controller._client = client

        await controller.close()
        client.delete.assert_awaited_once_with("http://localhost:8080/sessions/sess-close")
        assert (
            controller.transcript_dir
            == "/tmp/ws/.monkeybot/transcripts/20260714T150000Z_sess-close"
        )
        client.aclose.assert_awaited_once()
        # Idempotent
        await controller.close()
        assert client.delete.await_count == 1

    asyncio.run(_run())
