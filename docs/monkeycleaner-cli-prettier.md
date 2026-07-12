# Monkeycleaner — cli-prettier / chat TUI diffs

Anti-slop review of the monkeybot working-tree diffs (chat TUI + realtime talk unification).

Reviewed: modified + untracked sources under `cli/`, `src/monkeybot/cli/`, `src/monkeybot/core/runtime/`, `src/monkeybot/gateway/realtime/`, `src/monkeybot/gateway/sse/`, plus related tests/docs.

---

## Slop

- ~~two full realtime clients~~ **DONE** — everything through `RealtimeSessionController` / `wire_encode.py`.
- ~~`ServerToolResultFrame` dual `name`/`tool`~~ **DONE** — single `name` field (parse still accepts legacy `tool` key).
- ~~`hasattr(pending_bus, "register_pending")`~~ **DONE** — typed as `PendingResponseBusPort | None`.
- ~~late Phase D imports~~ **DONE** — top-level wire imports; frame handling split into helpers.
- ~~Protocol / `chat_renderer` re-export mess~~ **DONE** — Protocols + `EVENT_KINDS` live in `chat_renderer.py`; `session_controller.py` is a thin re-export.
- ~~`hasattr(controller, "_emit_fn")` poke~~ **DONE** — public `set_emit()` on both controllers.
- ~~`_is_exit_command` back-compat alias~~ **DONE** — callers use `is_exit_command` / `encode_client_frame`.
- ~~`_consume_stream` / `_dispatch_turn_event` length~~ **DONE** — split into pump/backoff + small helpers.
- ~~`_handle_server_frame` length~~ **DONE** — audio/text/boundary helpers.
- ~~`run_talk_session` audio setup dup~~ **DONE** — `_setup_audio_devices`.
- ~~`_wait_for_health` copy-paste~~ **DONE** — shared `gateway_health.py`.
- ~~`chat_tui.py` god object~~ **DONE** — widgets in `chat_tui_widgets.py` (`ChatApp` remains in `chat_tui.py`).
- ~~demo screenshot artifact~~ **DONE** — deleted.

## Scalability

- ~~pending futures on close~~ **DONE** — `_close_session` calls `abandon_pending_cancel_all()`.
- ~~`_abandoned` unbounded~~ **DONE** — `deque(maxlen=64)`.
- ~~ConnectionClosed / audio send / PTT / interrupt / HITL swallow~~ **DONE** — log + emit where user-visible; shutdown close stays debug.
- ~~silent history backfill~~ **DONE** — warns via `_warn`.
- ~~OSC 52 clipboard swallow~~ **DONE** — `logger.exception` / debug when driver missing; UI already shows failure.

## Observability

- Project convention (gateway/runtime): `logger = logging.getLogger(__name__)` plus `logger.info/warning/exception(..., kv(...))` at boundaries.
- ~~`chat_session.py` stderr `_warn` / silent SSE paths~~ **DONE** — uses `logger` via `_warn` / reconnect / cancel warnings.
- ~~`realtime_loop.py` HITL unavailable / timeout / deny~~ **DONE** — logged with `kv(...)`.
- ~~`session_controller.py` outbound audio send~~ **DONE** — `logger.exception` + `stream_failed`.
