"""Textual widgets for the ``monkeybot chat`` TUI."""

from __future__ import annotations

import base64
import contextlib
import logging
import time
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.events import Key, Paste
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Collapsible, Markdown, Static, TextArea

logger = logging.getLogger(__name__)

from monkeybot_cli.chat_session import format_args_preview, format_schema_field_lines
from monkeybot_cli.chat_tool_display import format_tool_expand_body

_SPIN = "⠋⠙⠹⠸⠴⠦⠧⠇⠏"
_HISTORY_LIMIT = 500
_COMPOSER_PLACEHOLDER = "Message the agent — / commands · @ files · ! shell"


def _plain_title(text: str) -> Content:
    """Collapsible titles parse Textual markup; tool argv often contains ``[`` / ``*``."""
    return Content.from_text(text, markup=False)


def write_osc52_clipboard(app: App[object], text: str) -> bool:
    """Best-effort OSC 52 clipboard write via the Textual driver."""
    try:
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        seq = f"\x1b]52;c;{payload}\x07"
        driver = getattr(app, "_driver", None)
        if driver is None or not hasattr(driver, "write"):
            logger.debug("OSC 52 clipboard skipped: no Textual driver.write")
            return False
        driver.write(seq)
        return True
    except Exception:
        logger.exception("OSC 52 clipboard write failed")
        return False


class ThinkingLine(Static):
    """Transient dim status row in the transcript (thinking / summarizing)."""

    DEFAULT_CSS = """
    ThinkingLine {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 0 1;
        color: $muted;
        text-style: dim italic;
    }
    """

    def __init__(self, text: str = "thinking…", **kwargs: object) -> None:
        super().__init__(Text(text, style="dim italic"), **kwargs)  # type: ignore[arg-type]
        self._label = text
        self._spin_i = 0
        self._started = time.monotonic()
        self._timer: Timer | None = None

    def _animations_enabled(self) -> bool:
        app = self.app
        return bool(getattr(app, "animations_enabled", True))

    def on_mount(self) -> None:
        if self._animations_enabled():
            self._timer = self.set_interval(0.25, self._tick)
        else:
            # Elapsed seconds without spinner glyph.
            self._timer = self.set_interval(1.0, self._tick)
        self._tick()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def set_text(self, text: str) -> None:
        self._label = text
        self._render_line()

    def _tick(self) -> None:
        self._spin_i += 1
        self._render_line()

    def _render_line(self) -> None:
        elapsed = max(0, int(time.monotonic() - self._started))
        if self._animations_enabled():
            glyph = _SPIN[self._spin_i % len(_SPIN)]
            self.update(Text(f"{glyph} {self._label}  {elapsed}s", style="dim italic"))
        else:
            self.update(Text(f"{self._label}  {elapsed}s", style="dim italic"))


class ThinkingTrace(Vertical):
    """OpenCode-style thinking block: ``Thinking...`` / body / ``...done thinking.``"""

    DEFAULT_CSS = """
    ThinkingTrace {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 0 1;
    }
    ThinkingTrace > .header {
        height: 1;
        color: $muted;
        text-style: bold dim;
    }
    ThinkingTrace > .body {
        height: auto;
        color: $muted;
        text-style: dim;
    }
    ThinkingTrace > .footer {
        height: 1;
        color: $muted;
        text-style: bold dim;
        display: none;
    }
    ThinkingTrace.-done > .footer {
        display: block;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._raw = ""

    def compose(self) -> ComposeResult:
        yield Static("Thinking...", classes="header")
        yield Static("", classes="body")
        yield Static("...done thinking.", classes="footer")

    def append_delta(self, chunk: str) -> None:
        if not chunk:
            return
        self._raw += chunk
        with contextlib.suppress(NoMatches):
            self.query_one(".body", Static).update(Text(self._raw, style="dim"))

    def finish(self) -> None:
        self.add_class("-done")

    def reopen(self) -> None:
        """Allow late thinking deltas to continue this block after a premature close."""
        self.remove_class("-done")


class SystemLine(Static):
    """Dim full-width system / error line."""

    DEFAULT_CSS = """
    SystemLine {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 0 1;
        color: $muted;
    }
    SystemLine.error {
        color: $error;
    }
    """

    def __init__(self, body: str, *, error: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.body = body
        self.mounted_at = datetime.now()
        if error:
            self.add_class("error")

    def on_mount(self) -> None:
        style = "red" if self.has_class("error") else "dim"
        self.update(Text(self.body, style=style))


class GroundingBlock(Vertical):
    """Grounding sources with clickable markdown links."""

    DEFAULT_CSS = """
    GroundingBlock {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 0 1;
    }
    GroundingBlock > Markdown {
        height: auto;
        margin: 0;
        padding: 0;
        background: transparent;
        color: $muted;
    }
    """

    def __init__(self, markdown: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.markdown = markdown
        self.mounted_at = datetime.now()

    def compose(self) -> ComposeResult:
        yield Markdown(self.markdown, open_links=True)


class EarlierTurns(Collapsible):
    """Summary of trimmed older transcript widgets."""

    DEFAULT_CSS = """
    EarlierTurns {
        width: 1fr;
        height: auto;
        padding: 0 1;
        margin-top: 1;
        color: $muted;
    }
    EarlierTurns > Contents {
        height: auto;
        padding: 0 1 1 3;
        color: $disabled;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        self.omitted = 0
        self.digest_lines: list[str] = []
        self._body = Static("", classes="earlier-body")
        super().__init__(
            self._body,
            title=_plain_title("  0 earlier turns"),  # type: ignore[arg-type]
            collapsed=True,
            collapsed_symbol="▶",
            expanded_symbol="▼",
            **kwargs,  # type: ignore[arg-type]
        )

    def absorb(self, label: str) -> None:
        self.omitted += 1
        self.digest_lines.append(label)
        if len(self.digest_lines) > 40:
            self.digest_lines = self.digest_lines[-40:]
        noun = "turn" if self.omitted == 1 else "turns"
        self.title = _plain_title(f"  {self.omitted} earlier {noun}")  # type: ignore[assignment]
        preview = "\n".join(self.digest_lines[-20:])
        self._body.update(Text(preview, style="dim"))


class UserTurn(Static):
    """Single-column user message."""

    DEFAULT_CSS = """
    UserTurn {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 0 1;
        color: $foreground;
    }
    """

    def __init__(self, body: str, *, show_timestamp: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.body = body
        self.mounted_at = datetime.now()
        self.show_timestamp = show_timestamp

    def on_mount(self) -> None:
        self.refresh_label()

    def refresh_label(self) -> None:
        content = Text()
        if self.show_timestamp:
            content.append(self.mounted_at.strftime("%H:%M:%S "), style="dim")
        content.append("you\n", style="bold")
        content.append(self.body)
        self.update(content)


class AssistantTurn(Vertical):
    """Single-column assistant reply with live markdown + streaming cursor."""

    DEFAULT_CSS = """
    AssistantTurn {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 0 1;
    }
    AssistantTurn > .role {
        height: 1;
        color: $muted;
        text-style: dim;
    }
    AssistantTurn > Markdown {
        height: auto;
        margin: 0;
        padding: 0;
        background: transparent;
        color: $assistant;
    }
    AssistantTurn > .cursor {
        height: 1;
        color: $accent;
    }
    AssistantTurn.-done > .cursor {
        display: none;
    }
    """

    def __init__(self, *, show_timestamp: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._raw = ""
        self._pending = ""
        self._streaming = True
        self._flush_timer: Timer | None = None
        self.mounted_at = datetime.now()
        self.show_timestamp = show_timestamp

    def compose(self) -> ComposeResult:
        yield Static(self._role_label(), classes="role")
        yield Markdown("", open_links=False, classes="body")
        yield Static("▍", classes="cursor")

    def _role_label(self) -> str:
        if self.show_timestamp:
            return f"{self.mounted_at.strftime('%H:%M:%S')}  assistant"
        return "assistant"

    def refresh_label(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(".role", Static).update(self._role_label())

    def on_mount(self) -> None:
        # Flush timer is started lazily on first delta (and paused when drained).
        return

    def append_delta(self, chunk: str) -> None:
        if not chunk:
            return
        self._raw += chunk
        self._pending += chunk
        app = self.app
        if not bool(getattr(app, "animations_enabled", True)):
            self._flush_markdown()
            return
        if self._flush_timer is None and self.is_attached:
            self._flush_timer = self.set_interval(0.05, self._flush_markdown)

    def finish(self) -> None:
        self._streaming = False
        if self.is_attached:
            self._flush_markdown()
        self.add_class("-done")
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

    def _flush_markdown(self) -> None:
        if not self._pending:
            if self._flush_timer is not None:
                self._flush_timer.stop()
                self._flush_timer = None
            return
        chunk = self._pending
        self._pending = ""
        nodes = self.query(Markdown)
        if nodes:
            # Append-only: O(chunk) vs full reparse via update(_raw).
            nodes.first().append(chunk)
        if not self._pending and self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None


class ToolCallBlock(Collapsible):
    """Collapsed grey tool row; expand for Command/Result sections."""

    DEFAULT_CSS = """
    ToolCallBlock {
        width: 1fr;
        height: auto;
        background: transparent;
        border-top: none;
        padding: 0 1;
        margin-top: 0;
        color: $muted;
    }
    ToolCallBlock > Contents {
        width: 1fr;
        height: auto;
        padding: 0 1 1 3;
        color: $disabled;
    }
    ToolCallBlock.running {
        color: $muted;
    }
    ToolCallBlock.ok {
        color: $muted;
    }
    ToolCallBlock.error {
        color: $tool-error;
    }
    ToolCallBlock .tool-detail {
        width: 1fr;
        height: auto;
        margin: 0;
        padding: 0;
        background: transparent;
        color: $disabled;
    }
    """

    def __init__(
        self,
        title: str,
        *,
        tool: str = "",
        args: dict[str, object] | None = None,
        call_id: str = "",
        **kwargs: object,
    ) -> None:
        self.display_label = title
        self.tool_name = tool
        self.tool_args = dict(args or {})
        self.call_id = call_id
        self.status = "running"
        self.mounted_at = datetime.now()
        self._spin_i = 0
        self._spin_timer: Timer | None = None
        self._detail = Markdown(
            format_tool_expand_body(tool, self.tool_args),
            open_links=False,
            classes="tool-detail",
        )
        super().__init__(
            self._detail,
            title=_plain_title(f"  {title}"),  # type: ignore[arg-type]
            collapsed=True,
            collapsed_symbol="▶",
            expanded_symbol="▼",
            classes="running",
            **kwargs,  # type: ignore[arg-type]
        )

    def on_mount(self) -> None:
        app = self.app
        if bool(getattr(app, "animations_enabled", True)):
            self._spin_timer = self.set_interval(0.08, self._tick_spinner)
            self._tick_spinner()
        else:
            self.title = _plain_title(f"  {self.display_label}")  # type: ignore[assignment]

    def _tick_spinner(self) -> None:
        if self.status != "running":
            return
        glyph = _SPIN[self._spin_i % len(_SPIN)]
        self._spin_i += 1
        self.title = _plain_title(f"  {glyph} {self.display_label}")  # type: ignore[assignment]

    def mark_finished(self, *, error: object = None, result: str = "") -> None:
        self.status = "error" if error else "ok"
        self.remove_class("running")
        self.add_class(self.status)
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        mark = "✗" if error else "✓"
        self.title = _plain_title(f"  {mark} {self.display_label}")  # type: ignore[assignment]
        self._detail.update(
            format_tool_expand_body(
                self.tool_name,
                self.tool_args,
                result=result,
                error=error,
            )
        )


class EmptyHint(Static):
    """First-run empty state."""

    DEFAULT_CSS = """
    EmptyHint {
        width: 1fr;
        height: 100%;
        padding: 0 2;
        color: $muted;
        content-align: center middle;
        text-align: center;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(id="empty-hint", **kwargs)  # type: ignore[arg-type]

    def on_mount(self) -> None:
        self.update(Text("Welcome to monkeybot", style="dim"))


class HitlCard(Static):
    """Inline HITL prompt in the transcript (confirm or elicit)."""

    DEFAULT_CSS = """
    HitlCard {
        width: 1fr;
        height: auto;
        margin-top: 1;
        padding: 1 2;
        border-left: tall $warning;
        background: $hitl-surface;
        color: $hitl-text;
    }
    """

    def __init__(
        self,
        prompt: str,
        *,
        hitl_kind: str = "confirm",
        tool_name: str = "",
        arguments: dict | None = None,
        schema: dict | None = None,
        timeout_sec: float | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.prompt = prompt
        self.hitl_kind = hitl_kind
        self.tool_name = tool_name
        self.arguments = dict(arguments) if arguments else {}
        self.schema = dict(schema) if isinstance(schema, dict) else None
        self.timeout_sec = float(timeout_sec) if timeout_sec is not None else None
        self.mounted_at = datetime.now()
        self._timer: Timer | None = None
        self._expired = False

    def on_mount(self) -> None:
        self._render_card()
        if self.timeout_sec is not None and self.timeout_sec > 0:
            self._timer = self.set_interval(1.0, self._tick_timeout)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _remaining_sec(self) -> float | None:
        if self.timeout_sec is None:
            return None
        elapsed = (datetime.now() - self.mounted_at).total_seconds()
        return max(0.0, self.timeout_sec - elapsed)

    def _timeout_line(self) -> str | None:
        remaining = self._remaining_sec()
        if remaining is None:
            return None
        total = int(remaining)
        mins, secs = divmod(total, 60)
        return f"timeout in {mins}:{secs:02d}"

    def _render_card(self) -> None:
        content = Text()
        if self.hitl_kind == "elicit":
            content.append("input needed\n", style="bold yellow")
        else:
            content.append("approval needed\n", style="bold yellow")
        content.append(self.prompt.rstrip() + "\n", style="yellow")
        if self.hitl_kind == "confirm" and self.tool_name:
            content.append(f"tool: {self.tool_name}\n", style="dim yellow")
            if self.arguments:
                content.append(
                    f"args: {format_args_preview(self.arguments)}\n",
                    style="dim yellow",
                )
        if self.schema:
            for line in format_schema_field_lines(self.schema):
                content.append(line + "\n", style="dim yellow")
        timeout_line = self._timeout_line()
        if timeout_line:
            content.append(timeout_line + "\n", style="dim")
        if self.hitl_kind == "elicit":
            hint = "Enter submit  ·  Ctrl-C cancel"
        else:
            hint = "y approve  ·  n deny  ·  Ctrl-C cancel"
        content.append(hint, style="dim")
        self.update(content)

    def _tick_timeout(self) -> None:
        if self._expired:
            return
        remaining = self._remaining_sec()
        if remaining is None:
            return
        if remaining <= 0:
            self._expired = True
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
            app = self.app
            resolve = getattr(app, "_resolve_hitl_timeout", None)
            if callable(resolve):
                resolve()
            return
        self._render_card()


class ComposerBusySpinner(Static):
    """One-glyph spinner beside the composer while the agent turn is running."""

    DEFAULT_CSS = """
    ComposerBusySpinner {
        width: 2;
        height: 1;
        min-height: 1;
        content-align: center middle;
        color: $muted;
        padding: 0;
    }
    ComposerBusySpinner.-busy {
        color: $accent;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(" ", **kwargs)  # type: ignore[arg-type]
        self._spin_i = 0
        self._timer: Timer | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if not busy:
            self.remove_class("-busy")
            self.update(" ")
            return
        self.add_class("-busy")
        self._spin_i = 0
        if bool(getattr(self.app, "animations_enabled", True)):
            self._timer = self.set_interval(0.08, self._tick)
            self._tick()
        else:
            self.update("●")

    def _tick(self) -> None:
        if not self._busy:
            return
        glyph = _SPIN[self._spin_i % len(_SPIN)]
        self._spin_i += 1
        self.update(glyph)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


_SHORTCUT_ROWS: tuple[tuple[str, str], ...] = (
    ("Enter", "Send message"),
    ("Ctrl+J / Alt+Enter / Shift+Enter", "Insert newline"),
    ("↑ / ↓", "Recall history / navigate palette"),
    ("Ctrl+R", "Reverse history search"),
    ("Tab", "Complete highlighted palette entry"),
    ("Esc", "Interrupt turn · dismiss palette/search · double-tap to recall last message"),
    ("Shift+Tab", "Cycle approval mode (normal → auto-approve → deny-confirms)"),
    ("Ctrl+C", "Cancel / clear composer / exit"),
    ("Ctrl+U", "Toggle usage line"),
    ("F1", "Toggle key hints"),
    ("PageUp / PageDown", "Scroll transcript"),
    ("y / n", "Approve / deny a confirmation prompt"),
    ("Space", "Push-to-talk (realtime sessions)"),
    ("@", "Fuzzy-pick a file to insert its path"),
    ("!", "Run a local shell command (not sent to the agent)"),
    ("/", "Slash commands — /help lists them all"),
    ("?", "Show this overlay (when composer is empty)"),
)


class ShortcutsScreen(ModalScreen[None]):
    """Full keyboard-shortcut reference, dismissed with Esc/q/?."""

    DEFAULT_CSS = """
    ShortcutsScreen {
        align: center middle;
    }
    ShortcutsScreen > Vertical {
        width: auto;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $border;
        background: $surface;
    }
    ShortcutsScreen .title {
        text-style: bold;
        margin-bottom: 1;
    }
    ShortcutsScreen .row {
        height: auto;
        color: $foreground;
    }
    ShortcutsScreen .hint {
        margin-top: 1;
        color: $muted;
        text-style: dim;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Close", show=False),
        Binding("q", "dismiss_screen", "Close", show=False),
        Binding("question_mark", "dismiss_screen", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Keyboard shortcuts", classes="title")
            width = max(len(key) for key, _ in _SHORTCUT_ROWS)
            for key, desc in _SHORTCUT_ROWS:
                yield Static(f"  {key.ljust(width)}   {desc}", classes="row")
            yield Static("Esc / q / ? to close", classes="hint")

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


class Composer(TextArea):
    """Growing multiline input: Enter sends, Shift+Enter/Ctrl+J newline, history, slash."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    BINDINGS = [
        Binding("shift+enter", "insert_newline", "Newline", show=False),
        Binding("ctrl+j", "insert_newline", "Newline", show=False),
        Binding("alt+enter", "insert_newline", "Newline", show=False),
        Binding("up", "history_up", "History", show=False),
        Binding("down", "history_down", "History", show=False),
        Binding("ctrl+r", "history_search", "Search", show=False),
        Binding("escape", "cancel_search", "Cancel search", show=False),
        Binding("ctrl+g", "cancel_search", "Cancel search", show=False),
        Binding("tab", "slash_complete", "Complete", show=False),
    ]

    def __init__(self, history: list[str] | None = None) -> None:
        super().__init__(
            "",
            id="prompt",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="indent",
            placeholder=_COMPOSER_PLACEHOLDER,
        )
        self._history = list(history or [])
        self._history_index: int | None = None
        self._draft = ""
        self._prefix_matches: list[str] | None = None
        self._prefix_index: int | None = None
        self._paste_guard = False
        self._search_mode = False
        self._search_query = ""
        self._search_draft = ""
        self._search_matches: list[str] = []
        self._search_index = 0

    @property
    def in_search(self) -> bool:
        return self._search_mode

    @property
    def search_query(self) -> str:
        return self._search_query

    def push_history(self, line: str) -> None:
        text = line.rstrip("\n")
        if text.strip():
            self._history.append(text)
            if len(self._history) > _HISTORY_LIMIT:
                self._history = self._history[-_HISTORY_LIMIT:]
        self._history_index = None
        self._prefix_matches = None
        self._prefix_index = None
        self._draft = ""

    def recall_last(self) -> None:
        """Load the most recent submitted message back into the composer for editing."""
        if not self._history:
            return
        self.load_text(self._history[-1])
        self.move_cursor(self.document.end)
        self._sync_height()

    def action_insert_newline(self) -> None:
        if self._search_mode:
            return
        self.insert("\n")
        self._sync_height()

    def action_slash_complete(self) -> None:
        if self._search_mode:
            return
        with contextlib.suppress(Exception):
            app = self.app
            if hasattr(app, "complete_slash_from_palette"):
                filled = app.complete_slash_from_palette()
                if filled:
                    self.load_text(filled)
                    self._sync_height()

    def action_history_search(self) -> None:
        if not self._history:
            return
        if not self._search_mode:
            self._search_mode = True
            self._search_draft = self.text
            self._search_query = ""
            self._search_matches = list(reversed(self._history))
            self._search_index = 0
            self._apply_search_match()
        else:
            if self._search_matches:
                self._search_index = (self._search_index + 1) % len(self._search_matches)
                self._apply_search_match()
        self._notify_status()

    def action_cancel_search(self) -> None:
        if not self._search_mode:
            return
        self._exit_search(restore_draft=True)

    def _apply_search_match(self) -> None:
        if self._search_matches:
            self.load_text(self._search_matches[self._search_index])
        else:
            self.load_text("")
        self._sync_height()

    def _refilter_search(self) -> None:
        q = self._search_query.lower()
        matches = [h for h in reversed(self._history) if q in h.lower()]
        self._search_matches = matches
        self._search_index = 0
        self._apply_search_match()
        self._notify_status()

    def _exit_search(self, *, restore_draft: bool, accept: bool = False) -> None:
        if not self._search_mode:
            return
        accepted = self.text if accept else None
        self._search_mode = False
        self._search_query = ""
        self._search_matches = []
        self._search_index = 0
        if restore_draft:
            self.load_text(self._search_draft)
        elif accept and accepted is not None:
            self.load_text(accepted)
        self._search_draft = ""
        self._sync_height()
        self._notify_status()

    def _notify_status(self) -> None:
        with contextlib.suppress(Exception):
            app = self.app
            if hasattr(app, "_refresh_status"):
                app._refresh_status()

    def action_history_up(self) -> None:
        if self._search_mode:
            return
        with contextlib.suppress(Exception):
            app = self.app
            if hasattr(app, "slash_palette_move") and app.slash_palette_move(-1):
                return
        row, _col = self.cursor_location
        if row > 0 or not self._history:
            self.action_cursor_up()
            return

        if self._prefix_matches is not None:
            if self._prefix_index is not None and self._prefix_index > 0:
                self._prefix_index -= 1
                self.load_text(self._prefix_matches[self._prefix_index])
                self._sync_height()
            return

        if self._history_index is not None:
            if self._history_index > 0:
                self._history_index -= 1
                self.load_text(self._history[self._history_index])
                self._sync_height()
            return

        self._draft = self.text
        prefix = self._draft
        if prefix.strip():
            matches = [h for h in self._history if h.startswith(prefix)]
            if matches:
                self._prefix_matches = matches
                self._prefix_index = len(matches) - 1
                self.load_text(matches[self._prefix_index])
                self._sync_height()
                return

        self._history_index = len(self._history) - 1
        self.load_text(self._history[self._history_index])
        self._sync_height()

    def action_history_down(self) -> None:
        if self._search_mode:
            return
        with contextlib.suppress(Exception):
            app = self.app
            if hasattr(app, "slash_palette_move") and app.slash_palette_move(1):
                return
        if self._prefix_matches is not None:
            row, _col = self.cursor_location
            last_row = max(0, self.document.line_count - 1)
            if row < last_row:
                self.action_cursor_down()
                return
            assert self._prefix_index is not None
            if self._prefix_index < len(self._prefix_matches) - 1:
                self._prefix_index += 1
                self.load_text(self._prefix_matches[self._prefix_index])
            else:
                self._prefix_matches = None
                self._prefix_index = None
                self.load_text(self._draft)
            self._sync_height()
            return

        if self._history_index is None:
            self.action_cursor_down()
            return
        row, _col = self.cursor_location
        last_row = max(0, self.document.line_count - 1)
        if row < last_row:
            self.action_cursor_down()
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.load_text(self._history[self._history_index])
        else:
            self._history_index = None
            self.load_text(self._draft)
        self._sync_height()

    async def _on_paste(self, event: Paste) -> None:
        self._paste_guard = True
        await super()._on_paste(event)
        self._sync_height()
        self.set_timer(0.1, self._clear_paste_guard)

    def _clear_paste_guard(self) -> None:
        self._paste_guard = False

    def _apply_at_completion(self) -> bool:
        """If the @ file palette is open, splice the picked path in and return True."""
        app = self.app
        complete = getattr(app, "complete_at_from_palette", None)
        if not callable(complete):
            return False
        try:
            result = complete()
        except Exception:
            logger.exception("@ file completion failed")
            return False
        if result is None:
            return False
        filled, cursor = result
        self.load_text(filled)
        self.move_cursor(cursor)
        self._sync_height()
        return True

    def _on_key(self, event: Key) -> None:
        if self._search_mode:
            self._handle_search_key(event)
            return
        if event.character == "?" and not self.text.strip():
            with contextlib.suppress(Exception):
                app = self.app
                show = getattr(app, "action_show_shortcuts", None)
                if callable(show):
                    show()
                    event.prevent_default()
                    event.stop()
                    return
        if event.key == "tab":
            if self._apply_at_completion():
                event.prevent_default()
                event.stop()
                return
            with contextlib.suppress(Exception):
                app = self.app
                if hasattr(app, "complete_slash_from_palette"):
                    filled = app.complete_slash_from_palette()
                    if filled:
                        self.load_text(filled)
                        self._sync_height()
                        event.prevent_default()
                        event.stop()
                        return
        if event.key == "enter":
            if self._paste_guard:
                self.insert("\n")
                self._sync_height()
                event.prevent_default()
                event.stop()
                return
            if self._apply_at_completion():
                event.prevent_default()
                event.stop()
                return
            text = self.text
            with contextlib.suppress(Exception):
                app = self.app
                if hasattr(app, "slash_submit_text"):
                    override = app.slash_submit_text(text)
                    if override is not None:
                        text = override
            self.clear()
            self._history_index = None
            self._prefix_matches = None
            self._prefix_index = None
            self._sync_height()
            self.post_message(self.Submitted(text))
            event.prevent_default()
            event.stop()
            return
        super()._on_key(event)
        self.call_after_refresh(self._sync_height)

    def _handle_search_key(self, event: Key) -> None:
        key = event.key
        if key == "enter":
            self._exit_search(restore_draft=False, accept=True)
            event.prevent_default()
            event.stop()
            return
        if key in {"escape", "ctrl+g"}:
            self._exit_search(restore_draft=True)
            event.prevent_default()
            event.stop()
            return
        if key == "ctrl+r":
            self.action_history_search()
            event.prevent_default()
            event.stop()
            return
        if key == "backspace":
            self._search_query = self._search_query[:-1]
            self._refilter_search()
            event.prevent_default()
            event.stop()
            return
        if event.is_printable and event.character:
            self._search_query += event.character
            self._refilter_search()
            event.prevent_default()
            event.stop()
            return
        event.prevent_default()
        event.stop()

    def _sync_height(self) -> None:
        lines = max(1, self.document.line_count)
        wrapped = max(lines, 1 + self.text.count("\n"))
        self.styles.height = min(8, max(1, wrapped))


class TranscriptPane(VerticalScroll):
    """Transcript scroller that syncs sticky auto-follow from scroll position."""

    def on_mount(self) -> None:
        # Stick to bottom as turns stream in; ChatApp releases on user scroll-up.
        self.anchor(True)

    def watch_scroll_y(self, old: float, new: float) -> None:
        # Must preserve Widget.watch_scroll_y behavior — overriding without this
        # leaves the scrollbar thumb stuck and skips _refresh_scroll.
        if self.show_vertical_scrollbar:
            self.vertical_scrollbar.position = new
        if self._anchored and self._anchor_released:
            self._check_anchor()
        if round(old) != round(new):
            self._refresh_scroll()
        app = self.app
        on_scroll = getattr(app, "_on_transcript_scroll", None)
        if callable(on_scroll):
            on_scroll()
