# Chat TUI — UX Improvements & Realtime Harness Unification

Status of this doc: analysis of the `cli-prettier` work (Textual chat TUI) on branch
`cursor/textual-chat-tui`, with proposals for the next iteration and for driving the
realtime (`monkeybot talk`) harness through the same interface.

## Where we are today

The chat rework landed a clean three-layer split:

- **`chat_session.py` — `ChatSessionController`**: owns HTTP session + SSE loop, never
  touches the terminal, emits `ChatUiEvent(kind, payload)` to whatever renderer is
  attached. HITL answered via futures, aborted turns tracked by abandoned request ids.
- **`chat_tui.py` — `ChatApp`**: Textual single-column transcript (user turns, streaming
  markdown assistant turns, collapsible tool blocks with spinners, HITL card), growing
  composer with file-backed history, top bar (provider/model) and status bar
  (context ring · hints).
- **`chat_tool_display.py` / `chat_status_bar.py`**: pure formatters shared by TUI and
  the plain (non-TTY / `MONKEYBOT_CHAT_PLAIN=1`) fallback path in `commands/chat.py`.

This event-driven decoupling is the single most valuable thing here — it is exactly the
seam that lets us plug in the realtime harness later.

---

## Part 1 — Design & UX improvements

### 1.1 Composer & input

| # | Improvement | Notes |
|---|-------------|-------|
| 1 | Slash-command palette with autocomplete | Only `/bye`, `/quit`, `/exit` exist. Add `/help`, `/new` (fresh session), `/model <name>`, `/usage` (toggle), `/copy` (last reply), `/export`. Show a completion popup when the composer starts with `/`. |
| 2 | Reverse history search | Up/Down history exists, but no `Ctrl-R` incremental search and no prefix-filtered recall (type `git` then Up). |
| 3 | Bracketed-paste safety | `Composer._on_key` submits on every `enter`; a multi-line paste will fire a submit per line. Handle Textual's `Paste` event and insert as a block. |
| 4 | Shift+Enter reliability | Many terminals don't deliver `shift+enter` (needs kitty keyboard protocol). Add `Ctrl+J` / `Alt+Enter` fallbacks and mention in hints. |
| 5 | Placeholder text | Empty composer should show a dim `Message the agent — / for commands` placeholder instead of relying only on the transcript empty-state. |
| 6 | Queue input while busy | Submitting during an active turn is silently ignored (`if self._turn_active: return`). Either queue the message and auto-send on turn end, or flash the status bar so the user knows why nothing happened. |

### 1.2 Transcript rendering

| # | Improvement | Notes |
|---|-------------|-------|
| 7 | Incremental markdown streaming | `AssistantTurn._flush_markdown` re-parses the entire accumulated reply every 50 ms (`Markdown.update(self._raw)`). This is O(n²) over long replies and visibly janky near the end. Use Textual's `Markdown.append()` / `MarkdownStream` for append-only updates. |
| 8 | Copy / export | Textual captures the mouse, so terminal-native selection needs Shift+drag and grabs decorations. Add bindings: `y` copy last assistant reply, `/export` write transcript to a file, optional OSC 52 clipboard write. |
| 9 | Transcript virtualization / trimming | Every turn mounts widgets forever; long sessions will degrade. Cap mounted turns (e.g. 200) and collapse older ones into a "N earlier turns" expander. |
| 10 | Tool result rendering | Expanded tool body is one dim `Text` blob truncated at 8 000 chars. Add per-kind renderers: syntax-highlighted code for `read_file`, unified diff view for edits, exit-code + tail for shell, link list for search. |
| 11 | Correlate tool start/finish by id | `_ev_tool_finished` pops `_open_tools[0]` (FIFO). Interleaved/parallel tool calls will mark the wrong block done. Thread `tool_call_id` through `ToolCallStarted`/`ToolCallResult` payloads and match on it. |
| 12 | Clickable grounding sources | Grounding results are a plain `SystemLine`; the OSC 8 hyperlink support from the old path was lost on the TUI path. Render as a `Markdown` widget with real links (or Textual `@click` actions). |
| 13 | Thinking line with elapsed time | `ThinkingLine` is static text. Show a spinner + elapsed seconds (`thinking… 12s`) so long tool-free turns don't look hung; optionally show streaming tok/s while deltas arrive. |
| 14 | Turn timestamps | Optional dim timestamps per turn (toggle via `/timestamps`), useful when scrolling back through long sessions. |
| 15 | Richer empty state | First-run screen could show agent name, workspace path, model, and 2–3 example prompts instead of only "Message the agent to start". |

### 1.3 Status bar, header, session state

| # | Improvement | Notes |
|---|-------------|-------|
| 16 | Colored context ring | `format_context_ring_plain` dropped the green/yellow/red threshold colors the ANSI path had. Re-add via Rich markup in the status bar — the color was the actual signal ("summarization imminent"). |
| 17 | Runtime usage toggle | Cost/token line only appears with `--usage` at launch. Make it a live toggle (`Ctrl+U` or `/usage`), and consider always showing session cost once it is nonzero. |
| 18 | Header content | Top bar shows `provider / model` only. Add agent name, short session id, and gateway target; these matter when several gateways/agents are running. |
| 19 | Connection state indicator | On SSE failure the app just dies with exit 1. Add auto-reconnect with backoff, a `⚠ reconnecting…` status-bar state, and replay of the `session_ready` handshake. |
| 20 | Session resume | Gateway sessions are addressable but chat always creates a new one. Add `--session <id>` / `/resume` with transcript backfill so a killed terminal doesn't lose the conversation. |
| 21 | Honest abort semantics | Ctrl-C abort is local-only (request id added to `_abandoned`; the server keeps computing). Show "turn aborted (server may still be finishing)" and, longer-term, add a real cancel endpoint to the gateway. |
| 22 | F1 hints during turns | `action_toggle_hints` refuses to run while a turn is active — an arbitrary restriction; hints are most needed mid-turn ("how do I cancel?"). |

### 1.4 HITL

| # | Improvement | Notes |
|---|-------------|-------|
| 23 | Structured elicitation | `Agent requests input (JSON or text)` is hostile. If `ActionRequiredEvent` carries a schema, render labeled fields; otherwise at least show what the agent asked for. |
| 24 | Explicit approve/deny affordance | The HITL card explains `y / n / Ctrl-C` but the answer is typed into the general composer. A focused two-button (or two-key, no-Enter) mode would prevent "typed a message, accidentally approved" (empty submit = approve today, which is risky). Consider making bare Enter *not* approve. |
| 25 | HITL timeout display | If the gateway enforces a confirmation timeout, count it down on the card. |

### 1.5 Theming & accessibility

| # | Improvement | Notes |
|---|-------------|-------|
| 26 | Light-terminal support | All colors are hardcoded dark-theme hex (`#0f1115` background etc.). Use Textual theme variables (`$surface`, `$text`, …) and ship dark/light themes; respect `textual` theme switching. |
| 27 | Reduced motion | Two always-on timers (0.05 s markdown flush + 0.08 s spinner per running tool) burn CPU and flicker on slow terminals. Pause flush timer when no deltas are pending; honor a `--no-animations` flag. |

### 1.6 Plain-path & code health

| # | Improvement | Notes |
|---|-------------|-------|
| 28 | Plain path lost the usage display | `_PlainRenderer._on_usage_updated` computes the ring string and throws it away (`_ = format_context_ring_plain(...)`). Either print it (e.g. after each turn) or delete the handler. |
| 29 | Fire-and-forget tasks | `_PlainRenderer` wraps spinner/activity calls in bare `asyncio.create_task(...)` without keeping references — tasks can be GC'd and output can interleave out of order. Await them in sequence via a small queue. |
| 30 | Remove back-compat shims | `ChatStatusBar = UsageStore`, `_chat_session = _plain_chat_session`, `_format_http_error` / `_is_exit_command` re-exports exist only for old tests. Update the tests and delete the aliases before merge. |
| 31 | Unify the two renderer dispatch styles | TUI uses a module-level handler dict; plain path uses `getattr(self, f"_on_{kind}")`. Define one `ChatRenderer` protocol with typed event methods that both implement — this becomes the contract the realtime renderer implements too. |

---

## Part 2 — Realtime harness on the same interface

### 2.1 The gap today

`monkeybot talk` is an entirely parallel stack that shares nothing with chat:

```
chat: cli/monkeybot_cli  → ChatSessionController → HTTP+SSE gateway (:8080)
talk: cli/monkeybot_cli  → RealtimeSessionController → WebSocket on same gateway (:8080)
```

- Talk (TTY and non-TTY) goes through `RealtimeSessionController` → `ChatApp` / plain
  renderer. Encode helpers live in `monkeybot_cli.realtime.wire_encode`;
  `src/monkeybot/cli/realtime_client.py` is a thin shim.
- The wire protocol (`gateway/realtime/wire.py`) is a typed frame set mapped onto
  `ChatUiEvent`s.

### 2.2 Why the current design makes this easy

`ChatApp` never talks HTTP — it consumes `ChatUiEvent`s from a controller. The realtime
work is therefore mostly **a second controller**, not a second UI:

```
                       ┌────────────────────────────┐
                       │        ChatApp (TUI)       │
                       │  + plain renderer fallback │
                       └────────────▲───────────────┘
                                    │ ChatUiEvent
              ┌─────────────────────┴─────────────────────┐
              │                                           │
  ChatSessionController                     RealtimeSessionController (new)
  (HTTP + SSE, request/reply turns)         (WebSocket frames, full duplex)
              │                                           │
        gateway :8080 (SSE + WS via realtime_main)
```

### 2.3 Frame → UI event mapping

Most server frames map directly onto events the TUI already handles:

| Realtime frame | `ChatUiEvent` | Notes |
|---|---|---|
| `connected` | `session_ready` | Also feed header (session id, formats). |
| `text_delta` | `assistant_start` (first) + `assistant_delta` | `is_final=True` → `turn_complete` for the current utterance. |
| binary audio chunk | *(new)* `audio_chunk` | Consumed by the audio player, plus a "speaking" state for the status bar. |
| `turn_boundary role=assistant` | `turn_complete` | Close the assistant markdown block. |
| `turn_boundary role=user` | *(new)* `user_utterance_start` | See transcription gap below. |
| `tool_call` | `tool_started` | See tool-result gap below. |
| `interrupted` | `turn_aborted` | Existing "Turn aborted" system line works as-is. |
| `error` | `turn_error` | |
| `session_ended` | `stream_ended` | |

Client direction: composer submit → `ClientTextFrame`; Ctrl-C while model is speaking →
`ClientInterruptFrame` (a *real* server-side interrupt — better semantics than chat's
local-only abort); `/bye` → `ClientCloseFrame`.

### 2.4 What the TUI needs to add for realtime

1. **Full-duplex turn model.** `ChatApp` gates the composer on `_turn_active` (one
   request/reply at a time). Realtime is barge-in: typing while the model speaks must be
   allowed and should trigger an interrupt. Make turn-gating a controller capability
   flag (`controller.turn_based: bool`) rather than hardcoded UI behavior.
2. **Voice status in the status bar.** States: `● listening` / `◌ muted (model
   speaking)` / `⏺ PTT held` / `▶ speaking`, plus a simple VU meter driven by
   `chunk_peak_db`. The half-duplex echo-gating state machine already exists in
   `RealtimeSessionController._mic_open`; it emits `voice_state` / `audio_chunk` events.
3. **Push-to-talk indication.** PTT uses a global key listener (pynput) since terminals
   have no key-up events — keep that mechanism, but reflect held/released in the TUI.
   Offer an in-TUI alternative (e.g. Space toggles mic when composer is empty) for
   environments where a global listener is unavailable (SSH). |
4. **Utterance-based transcript blocks.** Chat keys everything on `request_id`;
   realtime has none. `AssistantTurn` blocks should open on first `text_delta` and close
   on `is_final` / `turn_boundary` — the existing `state.assistant_started` reset logic
   is nearly identical.
5. **Audio device errors as `SystemLine`s** with the existing setup tips (PortAudio
   install hint, `--text` fallback) instead of raw stderr prints.

### 2.5 Wire-protocol gaps to fix server-side

These block full parity and are worth doing regardless of the TUI:

- **No tool result frame.** `ServerToolCallFrame` exists but nothing reports completion,
  so a `ToolCallBlock` could never leave the spinner state. Add `tool_result`
  (`call_id`, `error`, `result`) to `wire.py`.
- **No user-speech transcription frame.** Voice conversations show only the assistant
  side of the transcript. Gemini Live can return input transcriptions — add a
  `user_transcript` frame so the transcript shows both sides.
- **No usage/metrics frame.** The realtime gateway has a metrics module, but the client
  never sees token/cost data, so the context ring would sit at 0 %. Add a periodic
  `usage` frame mirroring the `/usage` REST shape (`parse_usage_response` can be
  reused as-is).
- **No HITL frames.** Realtime tool confirmation/elicitation doesn't exist on the wire.
  If realtime agents get gated tools, mirror `tool_confirmation` / `elicitation`
  frames; the TUI's HITL card then works unchanged.

### 2.6 Suggested phasing

1. **Phase A — consolidate the client code.** Move `realtime_client.py`, `audio_io.py`,
   `push_to_talk.py` from `src/monkeybot/cli/` into `cli/src/monkeybot_cli/` so there is
   one CLI package; dedupe exit-command parsing and gateway lifecycle helpers.
2. **Phase B — `RealtimeSessionController`.** Wrap the WebSocket loop in a controller
   emitting `ChatUiEvent`s (mapping above); keep a plain renderer for `--text` / CI.
   `monkeybot talk --text` now gets the full TUI for free.
3. **Phase C — voice UX.** Audio-state events, status-bar voice indicator, PTT
   reflection, interrupt-on-type. Entry point becomes `monkeybot chat --realtime`
   (or `talk` keeps its name but launches `ChatApp`).
4. **Phase D — protocol parity.** `tool_result`, `user_transcript`, `usage`, and
   (if needed) HITL frames in `wire.py` + gateway session, then light TUI wiring.

### 2.7 Gateway lifecycle unification

`run_chat` spawns/attaches the HTTP gateway on `:8080`; `run_talk_session` auto-starts a
separate realtime gateway on `ws://localhost:8787`. Fold both into the existing
spawn/attach/health-wait logic in `commands/chat.py` (config-driven ports, one
`--no-start-gateway` convention, shared log-file cleanup), so `chat` and `chat
--realtime` feel like the same product rather than two tools. **Done for local CLI:
both default to `runtime.port` (8080) and spawn `realtime_main`.**

---

## Priority shortlist

If only a handful of these get done next, do these:

1. Incremental markdown streaming (#7) — biggest perceived-performance win.
2. Tool start/finish correlation by id (#11) — correctness bug waiting to happen.
3. Colored context ring + connection state (#16, #19) — restores lost signal, adds trust.
4. Plain-path usage fix + shim cleanup (#28, #30) — cheap hygiene before merge.
5. Phase A+B of realtime unification — one CLI package, `RealtimeSessionController`,
   TUI for `talk --text`. Everything else in Part 2 builds on this.
