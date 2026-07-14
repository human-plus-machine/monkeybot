"""Textual TUI for ``monkeybot chat``."""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from monkeybot_cli.chat_session import (
    ChatSessionController,
    ChatUiEvent,
    HitlAnswer,
)
from monkeybot_cli.chat_status_bar import SessionUsageView, format_context_ring_markup, format_voice_status
from monkeybot_cli.chat_theme import MONKEYBOT_DARK, MONKEYBOT_LIGHT, resolve_theme_name
from monkeybot_cli.chat_tool_display import tool_collapsed_title

from monkeybot_cli.chat_tui_widgets import (
    AssistantTurn,
    Composer,
    ComposerBusySpinner,
    EarlierTurns,
    EmptyHint,
    GroundingBlock,
    HitlCard,
    SystemLine,
    ThinkingLine,
    ThinkingTrace,
    ToolCallBlock,
    TranscriptPane,
    UserTurn,
    _COMPOSER_PLACEHOLDER,
    write_osc52_clipboard,
)
from monkeybot_cli.exit_commands import is_exit_command
from monkeybot_cli.chat_renderer import SessionController

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 500
_PENDING_LIMIT = 5
_MAX_MOUNTED_TURNS = 200
_SLASH_PREFIX_RE = re.compile(r"^/\S*$")

_SLASH_SPECS: tuple[tuple[str, str], ...] = (
    ("/help", "Show commands and key hints"),
    ("/new", "Start a fresh session"),
    ("/resume", "Resume a session by id"),
    ("/usage", "Toggle token/cost usage line (Ctrl+U)"),
    ("/timestamps", "Toggle turn timestamps"),
    ("/copy", "Copy last assistant reply"),
    ("/export", "Export transcript to a markdown file"),
    ("/bye", "Exit chat"),
)


def gateway_host_label(base: str) -> str:
    """Short host:port label from a gateway base URL."""
    parsed = urlparse(base)
    host = parsed.hostname or parsed.netloc or base
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host or base


def format_topbar(
    *,
    agent_name: str,
    provider: str,
    model: str,
    session_id: str | None,
    gateway: str,
    width: int = 120,
) -> str:
    """Build top-bar text; prefer agent + short session id when truncating."""
    short = (session_id or "")[:8]
    parts = [agent_name, f"{provider}/{model}"]
    if short:
        parts.append(short)
    parts.append(gateway)
    line = " · ".join(parts)
    if width > 20 and len(line) > width:
        compact = [agent_name]
        if short:
            compact.append(short)
        compact.append(gateway)
        line = " · ".join(compact)
    return line


def parse_slash_command(line: str) -> tuple[str, str] | None:
    """Return ``(name, arg)`` for a leading slash command, else ``None``."""
    text = line.strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    name = parts[0][1:].lower()
    arg = parts[1] if len(parts) > 1 else ""
    return name, arg


def filter_slash_commands(prefix: str) -> list[tuple[str, str]]:
    text = prefix.strip()
    if not text.startswith("/"):
        return []
    lower = text.lower()
    return [(cmd, desc) for cmd, desc in _SLASH_SPECS if cmd.startswith(lower)]


def ensure_history_file(agent_root: Path) -> Path:
    path = agent_root / "data" / "chat_history"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    return path


def load_history_lines(agent_root: Path, *, limit: int = _HISTORY_LIMIT) -> list[str]:
    path = ensure_history_file(agent_root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.strip()][-limit:]


def append_history(agent_root: Path, line: str) -> None:
    path = ensure_history_file(agent_root)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


class ChatApp(App[int]):
    """Claude Code–style single-column chat TUI."""

    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }
    #topbar {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $background;
        color: $muted;
        text-style: dim;
    }
    #transcript {
        height: 1fr;
        padding: 0 1 1 1;
        background: $background;
        scrollbar-background: $background;
        scrollbar-color: $scrollbar;
    }
    #empty-hint {
        width: 100%;
        height: 100%;
        content-align: center middle;
        color: $disabled;
        text-style: dim;
    }
    #jump-bottom {
        height: 1;
        min-height: 1;
        width: 100%;
        border: none;
        background: $surface;
        color: $secondary;
        text-align: center;
        display: none;
    }
    #jump-bottom:hover {
        color: $foreground;
    }
    #jump-bottom:focus {
        border: none;
        text-style: bold;
    }
    #composer-wrap {
        height: auto;
        max-height: 16;
        min-height: 1;
        padding: 0 1;
        border-top: solid $border;
        background: $surface;
    }
    #composer-wrap.hitl {
        border-top: solid $warning;
    }
    #composer-row {
        height: auto;
        min-height: 1;
        width: 1fr;
        align: left middle;
    }
    #composer-busy {
        width: 2;
        height: 1;
        min-height: 1;
    }
    #slash-palette {
        height: auto;
        max-height: 6;
        background: $surface;
        color: $secondary;
        border: none;
        padding: 0;
        display: none;
    }
    #prompt {
        height: 1;
        min-height: 1;
        max-height: 8;
        width: 1fr;
        background: $surface;
        color: $foreground;
        border: none;
        padding: 0 1;
    }
    #prompt:focus {
        border: none;
    }
    #statusbar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $disabled;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "ctrl_c", "Cancel/Exit", show=False, priority=True),
        Binding("f1", "toggle_hints", "Hints", show=False),
        Binding("ctrl+u", "toggle_usage", "Usage", show=False),
        Binding("pageup", "transcript_page_up", "Scroll up", show=False),
        Binding("pagedown", "transcript_page_down", "Scroll down", show=False),
        Binding("y", "hitl_approve", "Approve", show=False, priority=True),
        Binding("n", "hitl_deny", "Deny", show=False, priority=True),
        Binding("space", "toggle_ptt", "PTT", show=False),
    ]

    show_hints: reactive[bool] = reactive(False)

    def __init__(
        self,
        *,
        base: str,
        agent_root: Path,
        provider: str,
        model: str,
        spawned_gateway: bool,
        model_provider: str | None = None,
        model_name: str | None = None,
        show_thinking: bool = False,
        verbose: bool = False,
        show_usage: bool = False,
        resume_session_id: str | None = None,
        animations_enabled: bool = True,
        theme_choice: str = "auto",
        controller: SessionController | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(MONKEYBOT_DARK)
        self.register_theme(MONKEYBOT_LIGHT)
        self.theme = resolve_theme_name(theme_choice)
        self.base = base
        self.agent_root = agent_root
        self.provider = provider
        self.model = model
        self.spawned_gateway = spawned_gateway
        self.show_usage = show_usage
        self.animations_enabled = animations_enabled
        self.theme_choice = theme_choice
        self._resume_session_id = resume_session_id
        if controller is not None:
            self._controller = controller
            self._controller.set_emit(self._on_controller_event)
        else:
            self._controller = ChatSessionController(
                base=base,
                model_provider=model_provider,
                model_name=model_name,
                show_thinking=show_thinking,
                verbose=verbose,
                show_usage=show_usage,
                emit=self._on_controller_event,
                resume_session_id=resume_session_id,
            )
        self._hitl_active = False
        self._hitl_kind: str | None = None
        self._turn_active = False
        self._open_tools: dict[str, ToolCallBlock] = {}
        self._anon_tools: list[ToolCallBlock] = []
        self._exit_code = 0
        self._assistant: AssistantTurn | None = None
        self._thinking: ThinkingLine | None = None
        self._thinking_trace: ThinkingTrace | None = None
        self._hitl_card: HitlCard | None = None
        self._hitl_status_flash: str | None = None
        self._ring_text = "[dim]○ 0%[/]"
        self._usage_text = ""
        self._session_cost_text = ""
        self._conn_text = ""
        self._voice_text = ""
        self._voice_level: float | None = None
        self._tui_ptt_held = False
        self._last_assistant_text = ""
        self._pending: list[str] = []
        self.show_timestamps = False
        self._turn_count = 0
        self._auto_scroll = True
        self._session_id: str | None = resume_session_id
        self.title = "monkeybot chat"

    def compose(self) -> ComposeResult:
        yield Static(
            format_topbar(
                agent_name=self.agent_root.name,
                provider=self.provider,
                model=self.model,
                session_id=self._session_id,
                gateway=gateway_host_label(self.base),
            ),
            id="topbar",
        )
        with TranscriptPane(id="transcript"):
            yield self._make_empty_hint()
        yield Button("↓ Jump to latest", id="jump-bottom", compact=True, flat=True)
        with Vertical(id="composer-wrap"):
            yield OptionList(id="slash-palette", compact=True)
            with Horizontal(id="composer-row"):
                yield ComposerBusySpinner(id="composer-busy")
                yield Composer(load_history_lines(self.agent_root))
        yield Static(self._status_line(), id="statusbar", markup=True)

    def _make_empty_hint(self) -> EmptyHint:
        return EmptyHint()

    def watch_show_hints(self, _value: bool) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#statusbar", Static).update(self._status_line())

    def _status_line(self) -> str:
        parts = [self._ring_text]
        if self._conn_text:
            parts.append(self._conn_text)
        if self._voice_text:
            parts.append(self._voice_text)
        if self._usage_text:
            parts.append(self._usage_text)
        elif self._session_cost_text:
            parts.append(self._session_cost_text)
        with contextlib.suppress(NoMatches):
            composer = self.query_one("#prompt", Composer)
            if composer.in_search:
                parts.append(f"bck-i-search: {composer.search_query}")
                return "  ·  ".join(parts)
        if self._pending:
            parts.append(f"queued · {len(self._pending)} waiting")
        if self._hitl_status_flash:
            parts.append(self._hitl_status_flash)
        if self._turn_active and not self._hitl_active:
            parts.append("working · Ctrl-C interrupt")
        elif self._hitl_active:
            if self._hitl_kind == "elicit":
                parts.append("Enter submit · Ctrl-C cancel")
            else:
                parts.append("y approve · n deny · Ctrl-C cancel")
        elif self.show_hints:
            parts.append(
                "Enter send · Ctrl+J newline · ↑ history · / commands · Ctrl+U usage"
            )
        else:
            parts.append("F1 hints")
        return "  ·  ".join(parts)

    def _set_composer_busy(self, busy: bool) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#composer-busy", ComposerBusySpinner).set_busy(busy)

    def _refresh_status(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#statusbar", Static).update(self._status_line())
        self._set_composer_busy(self._turn_active and not self._hitl_active)

    def _set_ring(self, text: str) -> None:
        self._ring_text = text
        self._refresh_status()

    def _refresh_topbar(self) -> None:
        with contextlib.suppress(NoMatches):
            width = self.size.width if self.size.width else 120
            self.query_one("#topbar", Static).update(
                format_topbar(
                    agent_name=self.agent_root.name,
                    provider=self.provider,
                    model=self.model,
                    session_id=self._session_id,
                    gateway=gateway_host_label(self.base),
                    width=width,
                )
            )

    def _set_usage_line(self, usage: dict[str, object]) -> None:
        try:
            cost = float(usage.get("cost_usd") or 0)
            ms = int(usage.get("duration_ms") or 0)
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            self._usage_text = f"in={inp} out={out} ${cost:.4f} {ms}ms"
            if cost > 0:
                self._session_cost_text = f"${cost:.4f}"
        except (TypeError, ValueError):
            self._usage_text = ""
        self._refresh_status()

    def _update_session_cost_from_view(self, usage: SessionUsageView) -> None:
        if usage.cost_usd > 0:
            self._session_cost_text = f"${usage.cost_usd:.4f}"
        if self.show_usage:
            self._usage_text = (
                f"in={usage.input_tokens} out={usage.output_tokens} "
                f"${usage.cost_usd:.4f}"
            )

    def _hide_empty_hint(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#empty-hint").remove()

    def _transcript(self) -> TranscriptPane:
        return self.query_one("#transcript", TranscriptPane)

    def _should_follow(self) -> bool:
        return self._auto_scroll

    def _set_transcript_follow(self, enabled: bool) -> None:
        """Enable/disable sticky bottom follow via Textual scroll anchoring."""
        self._auto_scroll = enabled
        with contextlib.suppress(NoMatches):
            transcript = self._transcript()
            if enabled:
                # anchor() scrolls to end with immediate=True (no deferred yank race).
                transcript.anchor(True)
            else:
                transcript.release_anchor()

    def _scroll_to_latest(self) -> None:
        """Stick to the bottom if follow is still enabled (re-checks at call time)."""
        if not self._auto_scroll:
            return
        with contextlib.suppress(NoMatches):
            transcript = self._transcript()
            # If the user has released the anchor, never call scroll_end/anchor —
            # Textual's scroll_end clears _anchor_released and yanks back to bottom.
            if transcript._anchor_released:
                self._auto_scroll = False
                self._refresh_jump_button()
                return
            # immediate=True avoids nesting an unconditional call_after_refresh.
            transcript.scroll_end(animate=False, immediate=True)

    def _follow_scroll(self) -> None:
        if not self._auto_scroll:
            return
        # Defer one frame so on_mount sizing / markdown layout are reflected in
        # max_scroll_y; the callback re-checks _auto_scroll before scrolling.
        self.call_after_refresh(self._scroll_to_latest)

    def _on_transcript_scroll(self) -> None:
        """Enable auto-scroll at the bottom; pause it when the user scrolls up."""
        transcript = self._transcript()
        at_end = transcript.is_vertical_scroll_end
        self._auto_scroll = at_end
        if at_end:
            # Re-engage compositor stickiness when the user returns to the bottom.
            if transcript.is_anchored and transcript._anchor_released:
                transcript._anchor_released = False
        else:
            # Ensure growth / follow cannot fight the user while reading history.
            if transcript.is_anchored and not transcript._anchor_released:
                transcript.release_anchor()
        self._refresh_jump_button()
    def _refresh_jump_button(self) -> None:
        with contextlib.suppress(NoMatches):
            btn = self.query_one("#jump-bottom", Button)
            btn.display = (not self._auto_scroll) and self._transcript().max_scroll_y > 0

    def action_jump_bottom(self) -> None:
        """Scroll to the latest turn and re-enable sticky auto-scroll."""
        self._set_transcript_follow(True)
        self._refresh_jump_button()

    def action_transcript_page_up(self) -> None:
        """Page the transcript up (composer keeps focus)."""
        with contextlib.suppress(NoMatches):
            self._transcript().action_page_up()

    def action_transcript_page_down(self) -> None:
        """Page the transcript down (composer keeps focus)."""
        with contextlib.suppress(NoMatches):
            self._transcript().action_page_down()

    @on(Button.Pressed, "#jump-bottom")
    def on_jump_bottom_pressed(self) -> None:
        self.action_jump_bottom()

    def _count_mounted_turns(self) -> int:
        return self._turn_count

    def _is_counted_turn(self, widget: object) -> bool:
        return isinstance(
            widget,
            (
                UserTurn,
                AssistantTurn,
                ToolCallBlock,
                SystemLine,
                HitlCard,
                GroundingBlock,
                ThinkingTrace,
            ),
        )

    def _turn_digest(self, widget: object) -> str:
        if isinstance(widget, UserTurn):
            preview = widget.body.strip().replace("\n", " ")[:60]
            return f"you: {preview}"
        if isinstance(widget, AssistantTurn):
            preview = widget._raw.strip().replace("\n", " ")[:60]
            return f"assistant: {preview}"
        if isinstance(widget, ThinkingTrace):
            preview = widget._raw.strip().replace("\n", " ")[:60]
            return f"thinking: {preview}"
        if isinstance(widget, ToolCallBlock):
            return f"tool: {widget.display_label}"
        if isinstance(widget, SystemLine):
            return f"system: {widget.body.strip().replace(chr(10), ' ')[:60]}"
        if isinstance(widget, GroundingBlock):
            return "grounding"
        if isinstance(widget, HitlCard):
            return "hitl"
        return "…"

    def _trim_old_turns(self) -> None:
        # Maintain a local counter — Textual remove() is deferred so DOM counts lag.
        while self._turn_count >= _MAX_MOUNTED_TURNS:
            transcript = self._transcript()
            earlier: EarlierTurns | None = None
            victim = None
            for child in transcript.children:
                if isinstance(child, EarlierTurns):
                    earlier = child
                    continue
                if child is self._assistant or child is self._thinking or child is self._thinking_trace or child is self._hitl_card:
                    continue
                if isinstance(child, ToolCallBlock) and (
                    child in self._open_tools.values() or child in self._anon_tools
                ):
                    continue
                if self._is_counted_turn(child) and not getattr(child, "_trimmed", False):
                    victim = child
                    break
            if victim is None:
                break
            victim._trimmed = True  # type: ignore[attr-defined]
            digest = self._turn_digest(victim)
            victim.remove()
            self._turn_count = max(0, self._turn_count - 1)
            if earlier is None:
                earlier = EarlierTurns()
                transcript.mount(earlier, before=0)
            earlier.absorb(digest)

    def _mount(self, widget: object) -> None:
        self._hide_empty_hint()
        if self._is_counted_turn(widget):
            self._trim_old_turns()
            self._turn_count += 1
        follow = self._should_follow()
        self._transcript().mount(widget)  # type: ignore[arg-type]
        if follow:
            self._follow_scroll()
        else:
            self._refresh_jump_button()

    def _show_thinking(self, text: str = "thinking…") -> None:
        if self._thinking is not None and self._thinking.is_attached:
            self._thinking.set_text(text)
            self._follow_scroll()
            return
        line = ThinkingLine(text)
        self._thinking = line
        self._mount(line)

    def _clear_thinking(self) -> None:
        if self._thinking is not None and self._thinking.is_attached:
            self._thinking.remove()
        self._thinking = None

    def _finish_thinking_trace(self, *, clear: bool = False) -> None:
        if self._thinking_trace is not None and self._thinking_trace.is_attached:
            if not self._thinking_trace.has_class("-done"):
                self._thinking_trace.finish()
        if clear:
            self._thinking_trace = None

    def _seal_thinking_trace(self) -> None:
        """Finish the current thinking block and start fresh on the next delta."""
        self._finish_thinking_trace(clear=True)
    def _mount_user(self, body: str) -> None:
        self._mount(UserTurn(body, show_timestamp=self.show_timestamps))

    def _mount_system(self, body: str, *, error: bool = False) -> None:
        self._mount(SystemLine(body, error=error))

    def _ensure_assistant(self) -> AssistantTurn:
        if self._assistant is None:
            self._clear_thinking()
            block = AssistantTurn(show_timestamp=self.show_timestamps)
            self._assistant = block
            self._mount(block)
        return self._assistant

    def _close_assistant(self) -> None:
        if self._assistant is not None:
            if self._assistant._raw:
                self._last_assistant_text = self._assistant._raw
            self._assistant.finish()
        self._assistant = None

    def _mount_tool(
        self,
        title: str,
        *,
        tool: str = "",
        args: dict[str, object] | None = None,
        call_id: str = "",
    ) -> ToolCallBlock:
        block = ToolCallBlock(title, tool=tool, args=args, call_id=call_id)
        self._mount(block)
        return block

    def _clear_open_tools(self) -> None:
        self._open_tools.clear()
        self._anon_tools.clear()

    def _register_open_tool(self, block: ToolCallBlock) -> None:
        if block.call_id:
            self._open_tools[block.call_id] = block
        else:
            self._anon_tools.append(block)

    def _pop_open_tool(self, call_id: str) -> ToolCallBlock | None:
        if call_id and call_id in self._open_tools:
            return self._open_tools.pop(call_id)
        if self._anon_tools:
            return self._anon_tools.pop(0)
        return None

    def _apply_timestamps(self) -> None:
        for child in self._transcript().children:
            if isinstance(child, UserTurn):
                child.show_timestamp = self.show_timestamps
                child.refresh_label()
            elif isinstance(child, AssistantTurn):
                child.show_timestamp = self.show_timestamps
                child.refresh_label()

    def _clear_hitl_card(self) -> None:
        if self._hitl_card is not None and self._hitl_card.is_attached:
            self._hitl_card.remove()
        self._hitl_card = None

    def _palette(self) -> OptionList:
        return self.query_one("#slash-palette", OptionList)

    def _hide_slash_palette(self) -> None:
        with contextlib.suppress(NoMatches):
            palette = self._palette()
            palette.display = False
            palette.clear_options()

    def _update_slash_palette(self, text: str) -> None:
        palette = self._palette()
        if self._hitl_active or not _SLASH_PREFIX_RE.match(text):
            palette.display = False
            palette.clear_options()
            return
        matches = filter_slash_commands(text)
        if not matches:
            palette.display = False
            palette.clear_options()
            return
        palette.set_options(
            [Option(f"{cmd}  {desc}", id=cmd) for cmd, desc in matches]
        )
        palette.highlighted = 0
        palette.display = True

    def slash_submit_text(self, text: str) -> str | None:
        """If the slash palette is open, return the highlighted command."""
        with contextlib.suppress(NoMatches):
            palette = self._palette()
            if not palette.display or palette.option_count == 0:
                return None
            idx = palette.highlighted if palette.highlighted is not None else 0
            option = palette.get_option_at_index(idx)
            if option.id:
                return str(option.id)
        return None

    def complete_slash_from_palette(self) -> str | None:
        return self.slash_submit_text(self.query_one("#prompt", Composer).text)

    def slash_palette_move(self, delta: int) -> bool:
        with contextlib.suppress(NoMatches):
            palette = self._palette()
            if not palette.display or palette.option_count == 0:
                return False
            idx = palette.highlighted if palette.highlighted is not None else 0
            idx = max(0, min(palette.option_count - 1, idx + delta))
            palette.highlighted = idx
            return True
        return False

    async def on_mount(self) -> None:
        self.query_one("#prompt", Composer).focus()
        self._hide_slash_palette()
        # Native Textual anchoring keeps the transcript at the bottom as content
        # grows, until the user scrolls away (see _on_transcript_scroll).
        self._set_transcript_follow(True)
        self._connect_session()

    @on(TextArea.Changed, "#prompt")
    def on_prompt_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, Composer) and not event.text_area.in_search:
            self._update_slash_palette(event.text_area.text)

    @work(exclusive=True, group="session")
    async def _connect_session(self) -> None:
        try:
            await self._controller.connect()
        except RuntimeError as exc:
            self._mount_system(str(exc), error=True)
            self._exit_code = 1
            self.exit(self._exit_code)
            return

    def _on_controller_event(self, event: ChatUiEvent) -> None:
        self.call_later(self._handle_event, event)

    def _handle_event(self, event: ChatUiEvent) -> None:
        handler = _TUI_EVENT_HANDLERS.get(event.kind)
        if handler is not None:
            handler(self, event.payload)

    def _ev_usage_updated(self, p: dict) -> None:
        usage = p.get("usage")
        if isinstance(usage, SessionUsageView):
            self._set_ring(format_context_ring_markup(usage))
            self._update_session_cost_from_view(usage)
            self._refresh_status()
        else:
            view = self._controller.usage.usage
            self._set_ring(format_context_ring_markup(view))
            if view is not None:
                self._update_session_cost_from_view(view)
                self._refresh_status()

    def _ev_session_ready(self, p: dict) -> None:
        sid = str(p.get("session_id") or "") or None
        self._session_id = sid
        self._refresh_topbar()

    def _ev_connection_state(self, p: dict) -> None:
        state = str(p.get("state") or "")
        if state == "reconnecting":
            attempt = p.get("attempt")
            self._conn_text = (
                f"[yellow]reconnecting…[/] ({attempt})"
                if attempt
                else "[yellow]reconnecting…[/]"
            )
        elif state == "connected":
            self._conn_text = ""
        self._refresh_status()

    def _ev_transcript_backfill(self, p: dict) -> None:
        messages = p.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return
        self._hide_empty_hint()
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            text = str(item.get("text") or "")
            if not text:
                continue
            if role == "user":
                self._mount(UserTurn(text, show_timestamp=self.show_timestamps))
            elif role == "assistant":
                turn = AssistantTurn(show_timestamp=self.show_timestamps)
                self._mount(turn)
                turn.append_delta(text)
                turn.finish()
                self._last_assistant_text = text
        self._follow_scroll()

    def _ev_thinking(self, p: dict) -> None:
        self._show_thinking(str(p.get("text") or "thinking…"))

    def _ev_thinking_clear(self, _p: dict) -> None:
        self._clear_thinking()

    def _ev_turn_started(self, _p: dict) -> None:
        self._turn_active = True
        self._assistant = None
        self._seal_thinking_trace()
        self._clear_open_tools()
        self._show_thinking("thinking…")
        self._refresh_status()

    def _ev_assistant_start(self, _p: dict) -> None:
        self._clear_thinking()

    def _ev_assistant_delta(self, p: dict) -> None:
        delta = str(p.get("delta", ""))
        if not delta:
            return
        self._ensure_assistant().append_delta(delta)
        self._follow_scroll()

    def _ev_tool_started(self, p: dict) -> None:
        self._close_assistant()
        self._clear_thinking()
        # Next thinking phase (e.g. after tools) should open a new block.
        self._seal_thinking_trace()
        raw_args = p.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        tool = str(p.get("tool") or "tool")
        label = str(p.get("label") or tool)
        call_id = str(p.get("call_id") or "")
        title = tool_collapsed_title(tool, label, args)
        block = self._mount_tool(title, tool=tool, args=args, call_id=call_id)
        self._register_open_tool(block)

    def _ev_tool_finished(self, p: dict) -> None:
        err = p.get("error")
        result = str(p.get("result") or "")
        call_id = str(p.get("call_id") or "")
        block = self._pop_open_tool(call_id)
        if block is None:
            tool = str(p.get("tool") or "tool")
            block = self._mount_tool(
                tool_collapsed_title(tool, tool, {}),
                tool=tool,
                call_id=call_id,
            )
        block.mark_finished(error=err, result=result)
        self._follow_scroll()

    def _ev_summarizing(self, p: dict) -> None:
        tokens = int(p.get("tokens") or 0)
        self._show_thinking(f"summarizing context ({tokens:,} tokens)")

    def _ev_summarized(self, p: dict) -> None:
        turns = int(p.get("turns") or 0)
        noun = "turn" if turns == 1 else "turns"
        self._show_thinking(f"summarized {turns} {noun}")

    def _ev_grounding(self, p: dict) -> None:
        self._clear_thinking()
        queries = p.get("search_queries") or []
        lines = ["**grounded search**"]
        if queries:
            q = ", ".join(f'"{q}"' for q in queries)
            lines[0] += f" — {q}"
        for source in (p.get("sources") or [])[:5]:
            if not isinstance(source, dict):
                continue
            title = str(source.get("title") or "").strip()
            uri = str(source.get("uri") or "").strip()
            if not uri:
                continue
            label = title or uri
            lines.append(f"- [{label}]({uri})")
        self._mount(GroundingBlock("\n".join(lines)))

    def _ev_turn_error(self, p: dict) -> None:
        self._close_assistant()
        self._finish_thinking_trace(clear=True)
        self._clear_thinking()
        self._mount_system(f"Error: {p.get('error')}", error=True)
        self._finish_turn_ui()

    def _ev_turn_complete(self, p: dict) -> None:
        self._close_assistant()
        self._finish_thinking_trace(clear=True)
        self._clear_thinking()
        usage = p.get("usage")
        if isinstance(usage, dict) and self.show_usage:
            self._set_usage_line(usage)
        self._finish_turn_ui()

    def _ev_turn_aborted(self, p: dict) -> None:
        self._close_assistant()
        self._finish_thinking_trace(clear=True)
        self._clear_thinking()
        cancel_ok = p.get("cancel_ok")
        if cancel_ok is True:
            msg = "Turn aborted — cancel sent; server may still finish"
        elif cancel_ok is False:
            msg = "Turn aborted locally (cancel failed)"
        else:
            msg = "Turn aborted — cancel sent; server may still finish"
        self._mount_system(msg)
        self._hitl_active = False
        self._hitl_kind = None
        self._hitl_status_flash = None
        self.query_one("#composer-wrap").remove_class("hitl")
        self._clear_hitl_card()
        self._finish_turn_ui()

    def _ev_hitl_required(self, p: dict) -> None:
        self._hitl_active = True
        kind = str(p.get("hitl_kind") or "confirm")
        self._hitl_kind = kind
        self._hitl_status_flash = None
        wrap = self.query_one("#composer-wrap")
        wrap.add_class("hitl")
        self._clear_hitl_card()
        raw_schema = p.get("schema")
        schema = dict(raw_schema) if isinstance(raw_schema, dict) else None
        raw_args = p.get("arguments")
        arguments = dict(raw_args) if isinstance(raw_args, dict) else {}
        timeout_raw = p.get("timeout_sec")
        timeout_sec: float | None
        try:
            timeout_sec = float(timeout_raw) if timeout_raw is not None else None
        except (TypeError, ValueError):
            timeout_sec = None
        card = HitlCard(
            str(p.get("prompt") or "Waiting for input…"),
            hitl_kind=kind,
            tool_name=str(p.get("tool_name") or ""),
            arguments=arguments,
            schema=schema,
            timeout_sec=timeout_sec,
        )
        self._hitl_card = card
        self._mount(card)
        self._hide_slash_palette()
        self._refresh_status()
        self.query_one("#prompt", Composer).focus()

    def _ev_hitl_failed(self, p: dict) -> None:
        self._mount_system(str(p.get("message") or "HITL failed"), error=True)
        self._clear_hitl_ui()

    def _ev_hitl_frontend_unsupported(self, p: dict) -> None:
        self._mount_system(
            f"Frontend tool '{p.get('name')}' requires a UI — not supported here",
            error=True,
        )

    def _ev_session_busy(self, _p: dict) -> None:
        self._mount_system("Session busy — wait for the current turn to finish")
        self._finish_turn_ui()

    def _ev_error(self, p: dict) -> None:
        self._mount_system(str(p.get("message") or "Error"), error=True)

    def _ev_stream_failed(self, p: dict) -> None:
        self._mount_system(str(p.get("message") or "Stream failed"), error=True)
        if p.get("fatal", True):
            self._exit_code = 1
            self.exit(self._exit_code)

    def _ev_stream_ended(self, _p: dict) -> None:
        self._exit_code = 1 if self._controller.stream_error else self._exit_code

    def _ev_thinking_trace(self, p: dict) -> None:
        # Legacy --show-thinking path; prefer thinking_block_* when present.
        self._show_thinking(f"thinking  {p.get('text')}")

    def _ev_thinking_block_delta(self, p: dict) -> None:
        text = str(p.get("text") or "")
        if not text:
            return
        self._clear_thinking()
        if self._thinking_trace is not None and self._thinking_trace.is_attached:
            # Provider may reopen thinking after text started; append to the same
            # block above the reply instead of mounting a stray card below it.
            if self._thinking_trace.has_class("-done"):
                self._thinking_trace.reopen()
        else:
            block = ThinkingTrace()
            self._thinking_trace = block
            self._mount(block)
        self._thinking_trace.append_delta(text)
        self._follow_scroll()

    def _ev_thinking_block_complete(self, _p: dict) -> None:
        # Keep the pointer so late re-entered deltas can reopen this block.
        self._finish_thinking_trace(clear=False)
        self._follow_scroll()

    def _ev_voice_state(self, p: dict) -> None:
        state = str(p.get("state") or "")
        level = p.get("level_db")
        level_f = float(level) if isinstance(level, (int, float)) else None
        self._voice_level = level_f
        self._voice_text = format_voice_status(state, level_f) if state else ""
        self._refresh_status()

    def _ev_audio_chunk(self, p: dict) -> None:
        level = p.get("level_db")
        if isinstance(level, (int, float)) and self._voice_text:
            state = "listening"
            if "PTT" in self._voice_text:
                state = "ptt_held"
            elif "speaking" in self._voice_text:
                state = "speaking"
            elif "muted" in self._voice_text:
                state = "muted"
            self._voice_text = format_voice_status(state, float(level))
            self._refresh_status()

    def _ev_device_error(self, p: dict) -> None:
        msg = str(p.get("message") or "Audio device error")
        hint = p.get("hint")
        body = msg if not hint else f"{msg}\n{hint}"
        self._mount_system(body, error=True)

    def _ev_user_transcript(self, p: dict) -> None:
        text = str(p.get("text") or "").strip()
        if not text:
            return
        if p.get("is_final", True):
            self._mount_user(text)
            self._clear_thinking()

    def _toggle_tui_ptt(self) -> None:
        if getattr(self._controller, "turn_based", True):
            return
        self._tui_ptt_held = not self._tui_ptt_held
        setter = getattr(self._controller, "set_ptt_held", None)
        if callable(setter):
            setter(self._tui_ptt_held)
        self._refresh_status()

    def _finish_turn_ui(self) -> None:
        self._turn_active = False
        self._assistant = None
        self._clear_open_tools()
        self._refresh_status()
        self.query_one("#prompt", Composer).focus()
        self.call_later(self._try_drain_pending)

    def _try_drain_pending(self) -> None:
        if self._turn_active or self._hitl_active or not self._pending:
            return
        value = self._pending.pop(0)
        self._refresh_status()
        self._send_user_message(value)

    def _clear_hitl_ui(self) -> None:
        self._hitl_active = False
        self._hitl_kind = None
        self._hitl_status_flash = None
        wrap = self.query_one("#composer-wrap")
        wrap.remove_class("hitl")
        self._clear_hitl_card()
        self._refresh_status()

    def _flash_hitl_status(self, message: str) -> None:
        self._hitl_status_flash = message
        self._refresh_status()
        self.set_timer(1.5, self._clear_hitl_status_flash)

    def _clear_hitl_status_flash(self) -> None:
        self._hitl_status_flash = None
        self._refresh_status()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("hitl_approve", "hitl_deny"):
            return bool(self._hitl_active and self._hitl_kind == "confirm")
        return True

    def action_hitl_approve(self) -> None:
        if self._hitl_active and self._hitl_kind == "confirm":
            self._finish_hitl_answer(HitlAnswer(approved=True, text="y"))

    def action_hitl_deny(self) -> None:
        if self._hitl_active and self._hitl_kind == "confirm":
            self._finish_hitl_answer(HitlAnswer(approved=False, text="n"))

    def _send_user_message(self, value: str) -> None:
        append_history(self.agent_root, value)
        composer = self.query_one("#prompt", Composer)
        composer.push_history(value)
        self._mount_user(value.strip())
        self._submit_message(value)

    @on(Composer.Submitted)
    def on_composer_submitted(self, event: Composer.Submitted) -> None:
        value = event.text
        self._hide_slash_palette()
        if self._hitl_active:
            self._resolve_hitl(value)
            return
        if not value.strip():
            return
        parsed = parse_slash_command(value)
        if parsed is not None:
            self._dispatch_slash(parsed[0], parsed[1])
            return
        if self._turn_active:
            if not getattr(self._controller, "turn_based", True):
                # Realtime barge-in: interrupt then send immediately.
                self._controller.abort_turn()
                self._turn_active = False
                self._close_assistant()
                self._clear_thinking()
            else:
                if len(self._pending) >= _PENDING_LIMIT:
                    self._mount_system(
                        f"Queue full ({_PENDING_LIMIT}) — wait for the current turn",
                        error=True,
                    )
                    return
                self._pending.append(value)
                self._refresh_status()
                return
        if self._controller.reconnecting or not self._controller.stream_alive:
            self._mount_system("Not connected — wait for reconnect", error=True)
            self._refresh_status()
            return
        self._send_user_message(value)

    def _dispatch_slash(self, name: str, arg: str) -> None:
        if name == "bye":
            self._goodbye_and_exit()
            return
        if name == "help":
            self._cmd_help()
            return
        if name == "new":
            self._cmd_new()
            return
        if name == "resume":
            self._cmd_resume(arg)
            return
        if name == "usage":
            self._cmd_usage()
            return
        if name == "timestamps":
            self._cmd_timestamps()
            return
        if name == "copy":
            self._cmd_copy()
            return
        if name == "export":
            self._cmd_export()
            return
        self._mount_system("Unknown command — try /help", error=True)

    def _cmd_help(self) -> None:
        lines = ["Commands:"]
        for cmd, desc in _SLASH_SPECS:
            lines.append(f"  {cmd}  —  {desc}")
        lines.append(
            "Keys: Enter send · Ctrl+J / Alt+Enter / Shift+Enter newline · "
            "↑ history · Ctrl+R search · Ctrl+U usage · F1 hints"
        )
        self._mount_system("\n".join(lines))

    def _cmd_new(self) -> None:
        if self._turn_active:
            self._mount_system("Wait for the current turn to finish before /new")
            return
        self._pending.clear()
        self._restart_session()

    def _cmd_resume(self, arg: str) -> None:
        sid = arg.strip()
        if not sid:
            self._mount_system("Usage: /resume <session_id>", error=True)
            return
        if self._turn_active:
            self._mount_system("Wait for the current turn to finish before /resume")
            return
        self._pending.clear()
        self._resume_session(sid)

    @work(exclusive=True, group="session")
    async def _restart_session(self) -> None:
        try:
            await self._controller.restart_session()
        except RuntimeError as exc:
            self._mount_system(str(exc), error=True)
            return
        self._reset_transcript_ui()
        self._mount_system("New session")
        self._refresh_status()
        self.query_one("#prompt", Composer).focus()

    @work(exclusive=True, group="session")
    async def _resume_session(self, session_id: str) -> None:
        try:
            await self._controller.resume_session(session_id)
        except RuntimeError as exc:
            self._mount_system(str(exc), error=True)
            return
        self._reset_transcript_ui(keep_empty_hint=False)
        self._mount_system(f"Resumed session {session_id[:8]}")
        self._refresh_status()
        self.query_one("#prompt", Composer).focus()

    def _reset_transcript_ui(self, *, keep_empty_hint: bool = True) -> None:
        transcript = self._transcript()
        for child in list(transcript.children):
            child.remove()
        self._assistant = None
        self._thinking = None
        self._thinking_trace = None
        self._hitl_card = None
        self._clear_open_tools()
        self._last_assistant_text = ""
        self._usage_text = ""
        self._session_cost_text = ""
        self._ring_text = "[dim]○ 0%[/]"
        self._turn_count = 0
        if keep_empty_hint:
            transcript.mount(self._make_empty_hint())

    def _cmd_usage(self) -> None:
        self.show_usage = not self.show_usage
        self._controller.show_usage = self.show_usage
        if self.show_usage:
            self._mount_system("Usage display on")
            self._refresh_usage()
        else:
            self._usage_text = ""
            self._mount_system("Usage display off")
            self._refresh_status()

    def _cmd_timestamps(self) -> None:
        self.show_timestamps = not self.show_timestamps
        self._apply_timestamps()
        state = "on" if self.show_timestamps else "off"
        self._mount_system(f"Timestamps {state}")

    @work
    async def _refresh_usage(self) -> None:
        await self._controller.refresh_usage()

    def _cmd_copy(self) -> None:
        text = self._last_assistant_text
        if not text.strip():
            self._mount_system("Nothing to copy — no assistant reply yet")
            return
        if write_osc52_clipboard(self, text):
            self._mount_system("Copied last assistant reply")
        else:
            self._mount_system("Clipboard write failed (OSC 52 unavailable)", error=True)

    def _cmd_export(self) -> None:
        try:
            path = self._write_transcript_export()
        except OSError as exc:
            self._mount_system(f"Export failed: {exc}", error=True)
            return
        self._mount_system(f"Exported transcript to {path}")

    def _write_transcript_export(self) -> Path:
        lines: list[str] = ["# monkeybot chat export", ""]
        for child in self._transcript().children:
            if isinstance(child, EarlierTurns):
                lines.extend(
                    [
                        f"## Earlier ({child.omitted} turns)",
                        "",
                        *child.digest_lines,
                        "",
                    ]
                )
            elif isinstance(child, UserTurn):
                lines.extend(["## You", "", child.body, ""])
            elif isinstance(child, AssistantTurn):
                lines.extend(["## Assistant", "", child._raw, ""])
            elif isinstance(child, ThinkingTrace):
                lines.extend(["## Thinking", "", child._raw, ""])
            elif isinstance(child, SystemLine):
                lines.extend([f"*{child.body}*", ""])
            elif isinstance(child, GroundingBlock):
                lines.extend([child.markdown, ""])
            elif isinstance(child, ToolCallBlock):
                lines.extend([f"### Tool: {child.display_label}", ""])
            elif isinstance(child, HitlCard):
                lines.extend([f"*HITL: {child.prompt}*", ""])
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.agent_root / "data" / f"chat_export_{ts}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    def _finish_hitl_answer(self, answer: HitlAnswer) -> None:
        self._clear_hitl_ui()
        with contextlib.suppress(NoMatches):
            composer = self.query_one("#prompt", Composer)
            composer.clear()
            composer._sync_height()
        self._controller.provide_hitl_answer(answer)

    def _resolve_hitl(self, value: str) -> None:
        text = value.strip()
        lower = text.lower()
        kind = self._hitl_kind or "confirm"
        if kind == "confirm":
            if not text:
                self._flash_hitl_status("press y or n")
                return
            if lower in ("y", "yes"):
                self._finish_hitl_answer(HitlAnswer(approved=True, text=text))
            elif lower in ("n", "no"):
                self._finish_hitl_answer(HitlAnswer(approved=False, text=text))
            else:
                self._finish_hitl_answer(HitlAnswer(approved=False, text=text))
            return
        if not text:
            return
        self._finish_hitl_answer(HitlAnswer(text=text))

    def _resolve_hitl_timeout(self) -> None:
        if not self._hitl_active:
            return
        self._clear_hitl_ui()
        self._mount_system("confirmation timed out")
        self._controller.provide_hitl_answer(HitlAnswer(cancelled=True))

    @work(exclusive=True, group="submit")
    async def _submit_message(self, message: str) -> None:
        await self._controller.submit(message)
        if not self._controller.stream_alive:
            self._exit_code = 1
            self.exit(self._exit_code)

    def action_toggle_hints(self) -> None:
        self.show_hints = not self.show_hints

    def action_toggle_usage(self) -> None:
        self._cmd_usage()


    def action_toggle_ptt(self) -> None:
        """Space toggles in-TUI PTT when composer is empty (realtime / SSH)."""
        with contextlib.suppress(NoMatches):
            composer = self.query_one("#prompt", Composer)
            if composer.text.strip() or getattr(self._controller, "turn_based", True):
                # Don't steal Space from normal typing / turn-based chat.
                composer.insert(" ")
                return
        self._toggle_tui_ptt()

    def action_ctrl_c(self) -> None:
        composer = self.query_one("#prompt", Composer)
        if composer.in_search:
            composer.action_cancel_search()
            return
        if self._hitl_active:
            self._finish_hitl_answer(HitlAnswer(cancelled=True))
            return
        if self._turn_active:
            self._controller.abort_turn()
            return
        if composer.text.strip():
            composer.clear()
            composer._sync_height()
            self._hide_slash_palette()
            return
        self._goodbye_and_exit()

    def _goodbye_and_exit(self) -> None:
        msg = (
            "Goodbye — shutting down gateway…"
            if self.spawned_gateway
            else "Goodbye."
        )
        self._mount_system(msg)
        self._close_session_and_exit()

    @work(exclusive=True)
    async def _close_session_and_exit(self) -> None:
        # Must not be named `_shutdown` — that shadows Textual.App._shutdown.
        await self._controller.close()
        report_dir = getattr(self._controller, "transcript_report_dir", None)
        if isinstance(report_dir, str) and report_dir:
            self._mount_system(f"Transcript report → {report_dir}")
            print(f"Transcript report → {report_dir}", flush=True)
        self._exit_code = 1 if self._controller.stream_error else self._exit_code
        self.exit(self._exit_code)

    async def on_unmount(self) -> None:
        try:
            await self._controller.close()
        except Exception:
            logger.debug("controller close failed during unmount", exc_info=True)


_TUI_EVENT_HANDLERS = {
    "usage_updated": ChatApp._ev_usage_updated,
    "session_ready": ChatApp._ev_session_ready,
    "connection_state": ChatApp._ev_connection_state,
    "transcript_backfill": ChatApp._ev_transcript_backfill,
    "thinking": ChatApp._ev_thinking,
    "thinking_clear": ChatApp._ev_thinking_clear,
    "turn_started": ChatApp._ev_turn_started,
    "assistant_start": ChatApp._ev_assistant_start,
    "assistant_delta": ChatApp._ev_assistant_delta,
    "tool_started": ChatApp._ev_tool_started,
    "tool_finished": ChatApp._ev_tool_finished,
    "summarizing": ChatApp._ev_summarizing,
    "summarized": ChatApp._ev_summarized,
    "grounding": ChatApp._ev_grounding,
    "turn_error": ChatApp._ev_turn_error,
    "turn_complete": ChatApp._ev_turn_complete,
    "turn_aborted": ChatApp._ev_turn_aborted,
    "hitl_required": ChatApp._ev_hitl_required,
    "hitl_failed": ChatApp._ev_hitl_failed,
    "hitl_frontend_unsupported": ChatApp._ev_hitl_frontend_unsupported,
    "session_busy": ChatApp._ev_session_busy,
    "error": ChatApp._ev_error,
    "stream_failed": ChatApp._ev_stream_failed,
    "stream_ended": ChatApp._ev_stream_ended,
    "thinking_trace": ChatApp._ev_thinking_trace,
    "thinking_block_delta": ChatApp._ev_thinking_block_delta,
    "thinking_block_complete": ChatApp._ev_thinking_block_complete,
    "voice_state": ChatApp._ev_voice_state,
    "audio_chunk": ChatApp._ev_audio_chunk,
    "device_error": ChatApp._ev_device_error,
    "user_transcript": ChatApp._ev_user_transcript,
}


def run_chat_tui(
    *,
    base: str,
    agent_root: Path,
    provider: str,
    model: str,
    spawned_gateway: bool,
    model_provider: str | None = None,
    model_name: str | None = None,
    show_thinking: bool = False,
    verbose: bool = False,
    show_usage: bool = False,
    resume_session_id: str | None = None,
    animations_enabled: bool = True,
    theme_choice: str = "auto",
    controller: SessionController | None = None,
) -> int:
    app = ChatApp(
        base=base,
        agent_root=agent_root,
        provider=provider,
        model=model,
        spawned_gateway=spawned_gateway,
        model_provider=model_provider,
        model_name=model_name,
        show_thinking=show_thinking,
        verbose=verbose,
        show_usage=show_usage,
        resume_session_id=resume_session_id,
        animations_enabled=animations_enabled,
        theme_choice=theme_choice,
        controller=controller,
    )
    result = app.run()
    return int(result) if isinstance(result, int) else 0
