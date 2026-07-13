"""Textual ChatApp smoke tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from textual.events import Paste

from monkeybot_cli.chat_session import ChatSessionController, ChatUiEvent
from monkeybot_cli.chat_tool_display import format_tool_expand_body, tool_collapsed_title
from monkeybot_cli.chat_tui import (
    AssistantTurn,
    ChatApp,
    Composer,
    HitlCard,
    SystemLine,
    ThinkingTrace,
    ToolCallBlock,
    UserTurn,
    _COMPOSER_PLACEHOLDER,
    filter_slash_commands,
    format_topbar,
    gateway_host_label,
    is_exit_command,
    load_history_lines,
    parse_slash_command,
)
from monkeybot_cli.chat_tui_widgets import ComposerBusySpinner


def test_is_exit_command() -> None:
    assert is_exit_command("/bye")
    assert is_exit_command("  /BYE ")
    assert is_exit_command("/quit")
    assert is_exit_command("/exit")
    assert is_exit_command("/bye.")
    assert not is_exit_command("hello")
    assert not is_exit_command("/help")


def test_parse_slash_command() -> None:
    assert parse_slash_command("/help") == ("help", "")
    assert parse_slash_command("  /export notes  ") == ("export", "notes")
    assert parse_slash_command("hello") is None
    assert parse_slash_command("") is None


def test_filter_slash_commands() -> None:
    assert ("/help", "Show commands and key hints") in filter_slash_commands("/h")
    assert all(cmd.startswith("/e") for cmd, _ in filter_slash_commands("/e"))
    assert filter_slash_commands("help") == []


def test_chat_app_constructs(tmp_path: Path) -> None:
    app = ChatApp(
        base="http://127.0.0.1:9",
        agent_root=tmp_path,
        provider="fake",
        model="fake-model",
        spawned_gateway=False,
    )
    assert app.provider == "fake"
    assert app.model == "fake-model"
    keys = {b.key for b in app.BINDINGS}
    assert "ctrl+c" in keys
    assert "ctrl+u" in keys
    assert "f1" in keys


def test_format_topbar_and_gateway_label() -> None:
    assert gateway_host_label("http://127.0.0.1:8080") == "127.0.0.1:8080"
    line = format_topbar(
        agent_name="demo",
        provider="fake",
        model="m",
        session_id="abcdef12-zzzz",
        gateway="127.0.0.1:8080",
    )
    assert "demo" in line
    assert "fake/m" in line
    assert "abcdef12" in line
    assert "127.0.0.1:8080" in line


def test_filter_slash_includes_resume() -> None:
    assert ("/resume", "Resume a session by id") in filter_slash_commands("/re")


def test_composer_placeholder_and_bindings() -> None:
    assert issubclass(Composer, __import__("textual.widgets", fromlist=["TextArea"]).TextArea)
    keys = {b.key for b in Composer.BINDINGS}
    assert "ctrl+j" in keys
    assert "alt+enter" in keys
    assert "ctrl+r" in keys
    composer = Composer(["alpha", "git status", "git commit"])
    assert composer.placeholder == _COMPOSER_PLACEHOLDER


def test_composer_prefix_history() -> None:
    composer = Composer(["alpha", "git status", "git commit", "hello"])
    composer.load_text("git")
    composer.action_history_up()
    assert composer.text == "git commit"
    composer.action_history_up()
    assert composer.text == "git status"
    composer.action_history_down()
    assert composer.text == "git commit"
    composer.action_history_down()
    assert composer.text == "git"


def test_tool_collapsed_title_shell() -> None:
    assert tool_collapsed_title("run_command", "run_command", {"command": "ls"}) == "Shell  ls"


def test_format_tool_expand_body_sections() -> None:
    text = format_tool_expand_body(
        "run_command",
        {"command": "ls"},
        result="a\nb",
    )
    assert "**Command**" in text
    assert "`ls`" in text
    assert "**Result**" in text
    assert "```" in text
    assert "a" in text


def test_format_tool_expand_body_read_fenced() -> None:
    text = format_tool_expand_body(
        "read_file",
        {"path": "src/foo.py"},
        result="def x():\n    pass\n",
    )
    assert "```python" in text
    assert "def x()" in text


def test_format_tool_expand_body_diff() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    text = format_tool_expand_body("edit_file", {"path": "x"}, result=diff)
    assert "```diff" in text


def test_load_history_lines(tmp_path: Path) -> None:
    hist = tmp_path / "data" / "chat_history"
    hist.parent.mkdir(parents=True)
    hist.write_text("one\ntwo\n", encoding="utf-8")
    assert load_history_lines(tmp_path) == ["one", "two"]


def test_tool_and_turn_flow(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
            show_usage=True,
        )

        def skip() -> None:
            return None

        app._connect_session = skip  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(ChatUiEvent("turn_started", {}))
            app._handle_event(ChatUiEvent("assistant_start", {}))
            # No assistant widget until first delta
            assert list(app.query(AssistantTurn)) == []
            app._handle_event(ChatUiEvent("assistant_delta", {"delta": "Checking.\n"}))
            await pilot.pause()
            assert len(list(app.query(AssistantTurn))) == 1
            app._handle_event(
                ChatUiEvent(
                    "tool_started",
                    {
                        "tool": "run_command",
                        "args": {"command": "ls"},
                        "display": "run_command — ls",
                        "call_id": "c1",
                    },
                )
            )
            app._handle_event(
                ChatUiEvent(
                    "tool_finished",
                    {
                        "tool": "run_command",
                        "result": "README.md\n",
                        "error": None,
                        "call_id": "c1",
                    },
                )
            )
            app._handle_event(ChatUiEvent("assistant_start", {}))
            app._handle_event(ChatUiEvent("assistant_delta", {"delta": "Done."}))
            app._handle_event(
                ChatUiEvent(
                    "turn_complete",
                    {
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cost_usd": 0.001,
                            "duration_ms": 42,
                        }
                    },
                )
            )
            await pilot.pause()
            tools = list(app.query(ToolCallBlock))
            assert len(tools) == 1
            assert tools[0].status == "ok"
            assert "Shell" in tools[0].display_label
            assert tools[0].collapsed is True
            assistants = list(app.query(AssistantTurn))
            assert len(assistants) == 2
            assert "in=10" in app._usage_text
            assert app._last_assistant_text == "Done."
            app._mount_user("hi")
            await pilot.pause()
            assert list(app.query(UserTurn))

            app._handle_event(
                ChatUiEvent(
                    "hitl_required",
                    {
                        "prompt": "Approve rm?",
                        "hitl_kind": "confirm",
                        "tool_name": "run_command",
                        "arguments": {"command": "rm -rf /tmp/x"},
                        "timeout_sec": 300,
                    },
                )
            )
            await pilot.pause()
            cards = list(app.query(HitlCard))
            assert len(cards) == 1
            assert cards[0].hitl_kind == "confirm"
            assert cards[0].tool_name == "run_command"
            rendered = str(cards[0].render())
            assert "approval needed" in rendered
            assert "y approve" in rendered
            assert "Enter approve" not in rendered

    asyncio.run(_run())


def test_hitl_elicit_schema_and_empty_enter(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        answers: list = []
        app._controller.provide_hitl_answer = answers.append  # type: ignore[method-assign]

        async with app.run_test() as pilot:
            app._handle_event(
                ChatUiEvent(
                    "hitl_required",
                    {
                        "prompt": "Pick a city",
                        "hitl_kind": "elicit",
                        "schema": {
                            "properties": {
                                "city": {"type": "string", "description": "City name"},
                            }
                        },
                        "timeout_sec": 300,
                    },
                )
            )
            await pilot.pause()
            cards = list(app.query(HitlCard))
            assert cards
            rendered = str(cards[0].render())
            assert "input needed" in rendered
            assert "city (string)" in rendered
            assert app._hitl_active is True

            # Empty Enter must not resolve elicitation
            app._resolve_hitl("")
            assert app._hitl_active is True
            assert answers == []

            app._resolve_hitl("Paris")
            assert app._hitl_active is False
            assert len(answers) == 1
            assert answers[0].text == "Paris"

    asyncio.run(_run())


def test_hitl_confirm_empty_enter_and_yn_keys(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        answers: list = []
        app._controller.provide_hitl_answer = answers.append  # type: ignore[method-assign]

        async with app.run_test() as pilot:
            app._handle_event(
                ChatUiEvent(
                    "hitl_required",
                    {"prompt": "Approve?", "hitl_kind": "confirm", "timeout_sec": 300},
                )
            )
            await pilot.pause()
            assert app._hitl_active is True

            app._resolve_hitl("")
            assert app._hitl_active is True
            assert answers == []
            assert app._hitl_status_flash == "press y or n"

            app.action_hitl_approve()
            assert app._hitl_active is False
            assert len(answers) == 1
            assert answers[0].approved is True

            answers.clear()
            app._handle_event(
                ChatUiEvent(
                    "hitl_required",
                    {"prompt": "Approve again?", "hitl_kind": "confirm"},
                )
            )
            await pilot.pause()
            app.action_hitl_deny()
            assert answers[0].approved is False

    asyncio.run(_run())


def test_hitl_timeout_cancels(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        answers: list = []
        app._controller.provide_hitl_answer = answers.append  # type: ignore[method-assign]

        async with app.run_test() as pilot:
            app._handle_event(
                ChatUiEvent(
                    "hitl_required",
                    {
                        "prompt": "Approve?",
                        "hitl_kind": "confirm",
                        "timeout_sec": 0.05,
                    },
                )
            )
            await pilot.pause()
            assert app._hitl_active is True
            # Force expiry via card tick path
            card = app._hitl_card
            assert card is not None
            card.mounted_at = card.mounted_at.replace(year=2000)
            card._tick_timeout()
            await pilot.pause()
            assert app._hitl_active is False
            assert answers and answers[0].cancelled is True
            systems = [s.body for s in app.query(SystemLine)]
            assert any("timed out" in body for body in systems)

    asyncio.run(_run())


def test_tool_finish_matches_call_id(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(ChatUiEvent("turn_started", {}))
            app._handle_event(
                ChatUiEvent(
                    "tool_started",
                    {"tool": "task", "label": "task", "args": {"task": "a"}, "call_id": "a1"},
                )
            )
            app._handle_event(
                ChatUiEvent(
                    "tool_started",
                    {"tool": "task", "label": "task", "args": {"task": "b"}, "call_id": "b1"},
                )
            )
            app._handle_event(
                ChatUiEvent(
                    "tool_finished",
                    {"tool": "task", "result": "b-done", "error": None, "call_id": "b1"},
                )
            )
            await pilot.pause()
            tools = list(app.query(ToolCallBlock))
            assert len(tools) == 2
            by_id = {t.call_id: t for t in tools}
            assert by_id["a1"].status == "running"
            assert by_id["b1"].status == "ok"
            app._handle_event(
                ChatUiEvent(
                    "tool_finished",
                    {"tool": "task", "result": "a-done", "error": None, "call_id": "a1"},
                )
            )
            await pilot.pause()
            assert by_id["a1"].status == "ok"

    asyncio.run(_run())


def test_assistant_stream_accumulates(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(ChatUiEvent("turn_started", {}))
            for i in range(20):
                app._handle_event(ChatUiEvent("assistant_delta", {"delta": f"w{i} "}))
            await pilot.pause()
            turns = list(app.query(AssistantTurn))
            assert len(turns) == 1
            assert turns[0]._raw.startswith("w0 ")
            assert "w19 " in turns[0]._raw
            app._handle_event(ChatUiEvent("turn_complete", {}))
            await pilot.pause()
            assert turns[0].has_class("-done")

    asyncio.run(_run())


def test_timestamps_toggle(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._mount_user("hello")
            await pilot.pause()
            user = list(app.query(UserTurn))[0]
            assert user.show_timestamp is False
            app.query_one("#prompt", Composer).post_message(
                Composer.Submitted("/timestamps")
            )
            await pilot.pause()
            assert app.show_timestamps is True
            assert user.show_timestamp is True

    asyncio.run(_run())


def test_grounding_mounts_markdown_links(tmp_path: Path) -> None:
    async def _run() -> None:
        from monkeybot_cli.chat_tui import GroundingBlock

        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(
                ChatUiEvent(
                    "grounding",
                    {
                        "search_queries": ["monkeys"],
                        "sources": [{"title": "Docs", "uri": "https://example.com"}],
                    },
                )
            )
            await pilot.pause()
            blocks = list(app.query(GroundingBlock))
            assert len(blocks) == 1
            assert "https://example.com" in blocks[0].markdown
            assert "[Docs]" in blocks[0].markdown

    asyncio.run(_run())


def test_trim_earlier_turns(tmp_path: Path) -> None:
    async def _run() -> None:
        from monkeybot_cli import chat_tui as mod
        from monkeybot_cli.chat_tui import EarlierTurns

        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        old = mod._MAX_MOUNTED_TURNS
        mod._MAX_MOUNTED_TURNS = 5
        try:
            async with app.run_test() as pilot:
                for i in range(8):
                    app._mount_user(f"msg-{i}")
                await pilot.pause()
                earlier = list(app.query(EarlierTurns))
                assert len(earlier) == 1
                assert earlier[0].omitted >= 3
                assert app._count_mounted_turns() <= 5
        finally:
            mod._MAX_MOUNTED_TURNS = old

    asyncio.run(_run())


def test_empty_hint_shows_welcome(tmp_path: Path) -> None:
    async def _run() -> None:
        from monkeybot_cli.chat_tui import EmptyHint

        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            hints = list(app.query(EmptyHint))
            assert len(hints) == 1
            await pilot.pause()
            assert "Welcome to monkeybot" in str(hints[0].render())

    asyncio.run(_run())


def test_slash_help_and_unknown(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            composer = app.query_one("#prompt", Composer)
            composer.post_message(Composer.Submitted("/help"))
            await pilot.pause()
            systems = [w.body for w in app.query(SystemLine)]
            assert any("Commands:" in body for body in systems)
            assert any("/copy" in body for body in systems)

            composer.post_message(Composer.Submitted("/foo"))
            await pilot.pause()
            systems = [w.body for w in app.query(SystemLine)]
            assert any("Unknown command" in body for body in systems)

    asyncio.run(_run())


def test_slash_export(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._mount_user("hello")
            app._last_assistant_text = "world"
            block = AssistantTurn()
            block._raw = "world"
            app._mount(block)
            await pilot.pause()
            app.query_one("#prompt", Composer).post_message(Composer.Submitted("/export"))
            await pilot.pause()
            exports = list((tmp_path / "data").glob("chat_export_*.md"))
            assert len(exports) == 1
            content = exports[0].read_text(encoding="utf-8")
            assert "## You" in content
            assert "hello" in content
            assert "## Assistant" in content
            assert "world" in content

    asyncio.run(_run())


def test_queue_while_busy(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        sent: list[str] = []

        def capture(message: str) -> None:
            sent.append(message)

        app._submit_message = capture  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._turn_active = True
            app.query_one("#prompt", Composer).post_message(Composer.Submitted("queued one"))
            await pilot.pause()
            assert app._pending == ["queued one"]
            assert "queued · 1 waiting" in app._status_line()
            assert sent == []

            app._handle_event(ChatUiEvent("turn_complete", {}))
            await pilot.pause()
            await pilot.pause()
            assert app._pending == []
            assert sent == ["queued one"]
            assert list(app.query(UserTurn))

    asyncio.run(_run())


def test_composer_busy_spinner_tracks_turn(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            spinner = app.query_one("#composer-busy", ComposerBusySpinner)
            assert spinner.busy is False
            assert "working" not in app._status_line()

            app._handle_event(ChatUiEvent("turn_started", {}))
            await pilot.pause()
            assert spinner.busy is True
            assert "working · Ctrl-C interrupt" in app._status_line()

            app._handle_event(ChatUiEvent("turn_complete", {}))
            await pilot.pause()
            assert spinner.busy is False
            assert "working" not in app._status_line()

    asyncio.run(_run())


def test_paste_inserts_without_submit(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        submitted: list[str] = []

        async with app.run_test() as pilot:
            composer = app.query_one("#prompt", Composer)
            composer.focus()

            def on_submitted(event: Composer.Submitted) -> None:
                submitted.append(event.text)

            composer.Submitted  # noqa: B018 — keep reference for type checkers
            app._send_user_message = lambda value: submitted.append(value)  # type: ignore[method-assign]

            await composer._on_paste(Paste("line1\nline2\nline3"))
            await pilot.pause()
            assert "line1\nline2\nline3" in composer.text
            assert submitted == []
            assert composer._paste_guard is True

    asyncio.run(_run())


def test_session_ready_updates_topbar(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:8080",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )

        def skip() -> None:
            return None

        app._connect_session = skip  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(ChatUiEvent("session_ready", {"session_id": "abcdefgh-1234"}))
            await pilot.pause()
            assert app._session_id == "abcdefgh-1234"
            line = format_topbar(
                agent_name=tmp_path.name,
                provider="fake",
                model="m",
                session_id=app._session_id,
                gateway=gateway_host_label(app.base),
            )
            assert "abcdefgh" in line
            assert "fake/m" in line

    asyncio.run(_run())


def test_connection_state_in_status(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )

        def skip() -> None:
            return None

        app._connect_session = skip  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(
                ChatUiEvent("connection_state", {"state": "reconnecting", "attempt": 2})
            )
            await pilot.pause()
            assert "reconnecting" in app._status_line()
            app._handle_event(ChatUiEvent("connection_state", {"state": "connected"}))
            await pilot.pause()
            assert "reconnecting" not in app._status_line()

    asyncio.run(_run())


def test_transcript_backfill_mounts_turns(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )

        def skip() -> None:
            return None

        app._connect_session = skip  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._handle_event(
                ChatUiEvent(
                    "transcript_backfill",
                    {
                        "messages": [
                            {"role": "user", "text": "hello"},
                            {"role": "assistant", "text": "hi there"},
                        ]
                    },
                )
            )
            await pilot.pause()
            assert len(list(app.query(UserTurn))) == 1
            assert len(list(app.query(AssistantTurn))) == 1
            assert app._last_assistant_text == "hi there"

    asyncio.run(_run())


def test_f1_hints_toggle_during_turn(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )

        def skip() -> None:
            return None

        app._connect_session = skip  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._turn_active = True
            assert app.show_hints is False
            app.action_toggle_hints()
            await pilot.pause()
            assert app.show_hints is True

    asyncio.run(_run())


def test_turn_aborted_honest_message(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="m",
            spawned_gateway=False,
        )

        def skip() -> None:
            return None

        app._connect_session = skip  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            app._turn_active = True
            app._handle_event(ChatUiEvent("turn_aborted", {"cancel_ok": True}))
            await pilot.pause()
            lines = [w.body for w in app.query(SystemLine)]
            assert any("cancel sent" in line for line in lines)

    asyncio.run(_run())


def test_restart_session_controller() -> None:
    async def _run() -> None:
        events: list[ChatUiEvent] = []
        controller = ChatSessionController(
            base="http://localhost:8080",
            emit=events.append,
            model_provider="fake",
            model_name="fake-model",
        )
        client = AsyncMock()
        create_resp = AsyncMock()
        create_resp.raise_for_status = lambda: None
        create_resp.json = lambda: {"session_id": "sess-2"}
        client.post = AsyncMock(return_value=create_resp)
        controller._client = client
        controller.session_id = "sess-1"
        controller._stream_task = asyncio.create_task(asyncio.sleep(60))
        controller._active_request_id = "req-1"
        controller._abandoned.append("old")

        await controller.restart_session()

        assert controller.session_id == "sess-2"
        assert controller._active_request_id is None
        assert list(controller._abandoned) == []
        assert controller.stream_alive is True
        client.post.assert_awaited()
        assert any(e.kind == "session_ready" and e.payload.get("session_id") == "sess-2" for e in events)
        assert any(e.kind == "connection_state" and e.payload.get("state") == "connected" for e in events)
        if controller._stream_task is not None:
            controller._stream_task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await controller._stream_task

    asyncio.run(_run())


def test_assistant_flush_timer_lazy_and_paused(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
            animations_enabled=True,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await pilot.pause()
            turn = AssistantTurn()
            app._mount(turn)
            await pilot.pause()
            assert turn._flush_timer is None
            turn.append_delta("hello")
            await pilot.pause()
            assert turn._flush_timer is not None
            # Drain pending via flush; timer should stop when empty.
            turn._flush_markdown()
            assert turn._flush_timer is None
            assert turn._raw == "hello"

    asyncio.run(_run())


def test_no_animations_immediate_flush_and_static_tool(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
            animations_enabled=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await pilot.pause()
            turn = AssistantTurn()
            app._mount(turn)
            await pilot.pause()
            turn.append_delta("hi")
            await pilot.pause()
            assert turn._flush_timer is None
            assert turn._pending == ""
            assert turn._raw == "hi"

            tool = ToolCallBlock("read x", tool="read_file", args={})
            app._mount(tool)
            await pilot.pause()
            assert tool._spin_timer is None
            assert "read" in tool.title or "x" in tool.title

    asyncio.run(_run())


def test_realtime_barge_in_skips_queue(monkeypatch) -> None:
    """When turn_based is False, active turn should not queue composer submits."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from monkeybot_cli.chat_tui import ChatApp, Composer

    aborted = {"n": 0}

    class FakeCtrl:
        turn_based = False
        stream_alive = True
        reconnecting = False
        stream_error = False
        show_usage = False
        usage = MagicMock(usage=None)
        session_id = "s1"

        def abort_turn(self) -> None:
            aborted["n"] += 1

        async def submit(self, message: str) -> None:
            return None

        async def connect(self, resume_session_id=None) -> None:
            return None

        async def close(self) -> None:
            return None

        def provide_hitl_answer(self, answer) -> None:
            return None

        def set_emit(self, emit) -> None:
            return None

        async def restart_session(self) -> None:
            return None

        async def resume_session(self, session_id: str) -> None:
            return None

        async def refresh_usage(self) -> None:
            return None

    app = ChatApp(
        base="http://127.0.0.1:8080",
        agent_root=Path("."),
        provider="fake",
        model="m",
        spawned_gateway=False,
        controller=FakeCtrl(),
    )
    app._turn_active = True
    sent: list[str] = []
    monkeypatch.setattr(app, "_send_user_message", lambda v: sent.append(v))
    monkeypatch.setattr(app, "_mount_system", lambda *a, **k: None)
    monkeypatch.setattr(app, "_close_assistant", lambda: None)
    monkeypatch.setattr(app, "_clear_thinking", lambda: None)
    monkeypatch.setattr(app, "_hide_slash_palette", lambda: None)
    event = Composer.Submitted("barge in now")
    app.on_composer_submitted(event)
    assert aborted["n"] == 1
    assert sent == ["barge in now"]
    assert app._pending == []


def test_thinking_trace_streams_and_finishes(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
            animations_enabled=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._ev_thinking_block_delta({"text": "The user said hello"})
            await pilot.pause()
            assert app._thinking_trace is not None
            assert "The user said hello" in app._thinking_trace._raw
            header = app._thinking_trace.query_one(".header")
            assert "Thinking" in str(header.render())
            assert not app._thinking_trace.has_class("-done")

            app._ev_thinking_block_delta({"text": " — reply briefly."})
            await pilot.pause()
            assert "reply briefly" in app._thinking_trace._raw

            app._ev_thinking_block_complete({})
            await pilot.pause()
            # Finished block stays mounted; pointer kept for late re-entry.
            assert app._thinking_trace is not None
            assert app._thinking_trace.has_class("-done")
            traces = list(app.query(ThinkingTrace))
            assert len(traces) == 1
            assert traces[0].has_class("-done")
            assert "The user said hello — reply briefly." in traces[0]._raw

            app._ev_turn_complete({})
            await pilot.pause()
            assert app._thinking_trace is None

    asyncio.run(_run())


def test_late_thinking_delta_reopens_same_block(tmp_path: Path) -> None:
    """Thinking that re-enters after assistant text must not mount below the reply."""

    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
            animations_enabled=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._ev_thinking_block_delta({"text": "ask what they'd like me to"})
            app._ev_thinking_block_complete({})
            await pilot.pause()
            assert app._thinking_trace is not None
            assert app._thinking_trace.has_class("-done")

            app._ev_assistant_start({})
            app._ev_assistant_delta({"delta": "Yes, I can write code."})
            await pilot.pause()
            assert app._assistant is not None

            # Late fragment that belongs to the earlier thought.
            app._ev_thinking_block_delta({"text": " build."})
            app._ev_thinking_block_complete({})
            await pilot.pause()

            traces = list(app.query(ThinkingTrace))
            assert len(traces) == 1
            assert "ask what they'd like me to build." in traces[0]._raw
            assert traces[0].has_class("-done")

            # Thinking block must still sit above the assistant turn.
            children = list(app._transcript().children)
            assert children.index(traces[0]) < children.index(app._assistant)

    asyncio.run(_run())


def test_scrollbar_thumb_tracks_scroll_position(tmp_path: Path) -> None:
    """TranscriptPane must update the scrollbar thumb when scroll_y changes."""

    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for i in range(40):
                app._mount(SystemLine(f"line {i} " + ("x" * 40)))
            await pilot.pause()
            transcript = app._transcript()
            assert transcript.is_vertical_scroll_end
            assert transcript.vertical_scrollbar.position == transcript.scroll_y

            transcript.scroll_relative(y=-20, animate=False, immediate=True)
            await pilot.pause()
            assert not transcript.is_vertical_scroll_end
            assert app._auto_scroll is False
            assert transcript.vertical_scrollbar.position == transcript.scroll_y

    asyncio.run(_run())


def test_jump_to_bottom_reenables_auto_scroll(tmp_path: Path) -> None:
    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for i in range(40):
                app._mount(SystemLine(f"line {i} " + ("x" * 40)))
            await pilot.pause()
            transcript = app._transcript()
            assert transcript.max_scroll_y > 0
            assert app._auto_scroll is True
            assert transcript.is_vertical_scroll_end

            transcript.scroll_home(animate=False)
            await pilot.pause()
            assert app._auto_scroll is False
            btn = app.query_one("#jump-bottom")
            assert btn.display is True

            await pilot.click("#jump-bottom")
            await pilot.pause()
            assert app._auto_scroll is True
            assert transcript.is_vertical_scroll_end
            assert btn.display is False

            # Streaming follow stays on while auto-scroll is enabled.
            app._mount(SystemLine("newest"))
            await pilot.pause()
            assert app._auto_scroll is True
            app._scroll_to_latest()
            await pilot.pause()
            assert transcript.is_vertical_scroll_end

    asyncio.run(_run())


def test_batch_mount_sticks_to_bottom(tmp_path: Path) -> None:
    """Rapid mounts must land at the true bottom after a single refresh."""

    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for i in range(40):
                app._mount(SystemLine(f"line {i} " + ("x" * 40)))
            await pilot.pause()
            transcript = app._transcript()
            assert transcript.max_scroll_y > 0
            assert app._auto_scroll is True
            assert transcript.is_vertical_scroll_end
            assert transcript.scroll_y == transcript.max_scroll_y

    asyncio.run(_run())


def test_follow_scroll_does_not_yank_after_scroll_up(tmp_path: Path) -> None:
    """Deferred follow must not jump back down after the user scrolls up."""

    async def _run() -> None:
        app = ChatApp(
            base="http://127.0.0.1:9",
            agent_root=tmp_path,
            provider="fake",
            model="fake-model",
            spawned_gateway=False,
        )
        app._connect_session = lambda: None  # type: ignore[method-assign]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for i in range(40):
                app._mount(SystemLine(f"line {i} " + ("x" * 40)))
            await pilot.pause()
            transcript = app._transcript()
            assert transcript.is_vertical_scroll_end

            # Schedule follow while still at bottom (auto=True), then scroll up
            # before the refresh callback runs — the old scroll_end path yanked.
            app._follow_scroll()
            transcript.scroll_relative(y=-20, animate=False, immediate=True)
            await pilot.pause()
            y_after_user = float(transcript.scroll_y)
            assert app._auto_scroll is False
            assert not transcript.is_vertical_scroll_end

            app._mount(SystemLine("new content while reading history"))
            app._follow_scroll()
            await pilot.pause()
            await pilot.pause()
            assert app._auto_scroll is False
            assert float(transcript.scroll_y) <= y_after_user + 0.5

    asyncio.run(_run())
