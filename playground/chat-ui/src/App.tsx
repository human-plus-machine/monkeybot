import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  consumeSseJson,
  createSession,
  fetchSessionUsage,
  openEventsStream,
  postReply,
  type GatewayJsonEvent,
  type SessionUsageResponse,
} from './gatewayClient'

type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
}

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** Pixels from the bottom of the scroll container still treated as "at bottom" for auto-follow. */
const BOTTOM_SLACK_PX = 72

function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight
}

function summarizeToolArgs(args: Record<string, unknown> | undefined, maxLen = 360): string {
  if (!args || typeof args !== 'object' || Object.keys(args).length === 0) {
    return '(no arguments)'
  }
  try {
    const s = JSON.stringify(args)
    return s.length > maxLen ? `${s.slice(0, maxLen)}…` : s
  } catch {
    return '(unserializable arguments)'
  }
}

function buildToolStartedChatBody(
  tool: string,
  label: string | undefined,
  args: Record<string, unknown> | undefined,
): string {
  const head = label && label !== tool ? `${tool} — ${label}` : tool
  return `→ ${head}\n\n${summarizeToolArgs(args)}`
}

function buildToolResultChatBody(tool: string, err: string | undefined, preview: string): string {
  if (err) {
    return `✗ ${tool}\n\n${err}`
  }
  const body = preview.trim() ? preview : '(empty result)'
  return `← ${tool}\n\n${body}`
}

function formatTokens(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

const DEFAULT_CONTEXT_WINDOW = 1_000_000

function ContextUsageRing({
  lastPromptTokens,
  contextWindowTokens,
}: {
  lastPromptTokens: number
  contextWindowTokens: number
}) {
  const cap = Math.max(1, contextWindowTokens)
  const frac = Math.min(1, Math.max(0, lastPromptTokens / cap))
  const pct = Math.round(frac * 100)
  const r = 13
  const stroke = 2.75
  const c = 2 * Math.PI * r
  const dash = frac * c
  const strokeColor = pct >= 92 ? 'var(--danger)' : pct >= 75 ? '#ca8a04' : '#4ade80'
  const tip = `Last completed model request: about ${formatTokens(lastPromptTokens)} prompt tokens of ${formatTokens(cap)} context window (${pct}%). Set MODEL_CONTEXT_WINDOW on the gateway to match your model. This UI does not trigger summarization automatically.`

  return (
    <div
      className="context-ring-wrap"
      role="img"
      aria-label={tip}
      title={tip}
    >
      <svg className="context-ring-svg" viewBox="0 0 32 32" aria-hidden>
        <circle className="context-ring-track" cx="16" cy="16" r={r} fill="none" strokeWidth={stroke} />
        <circle
          cx="16"
          cy="16"
          r={r}
          fill="none"
          strokeWidth={stroke}
          stroke={strokeColor}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${c}`}
          transform="rotate(-90 16 16)"
        />
      </svg>
      <span className="context-ring-label" aria-hidden>
        {pct}%
      </span>
    </div>
  )
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  const [status, setStatus] = useState<'disconnected' | 'connecting' | 'connected' | 'error'>(
    'disconnected',
  )
  const [statusNote, setStatusNote] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null)
  const [toolHint, setToolHint] = useState<string | null>(null)

  const [sessionUsage, setSessionUsage] = useState<SessionUsageResponse | null>(null)
  const [usageNote, setUsageNote] = useState('')

  const streamAbortRef = useRef<AbortController | null>(null)
  const streamBufRef = useRef('')
  const messagesScrollRef = useRef<HTMLDivElement | null>(null)
  /** When true, new content scrolls the message list to the bottom. Set false if the user scrolls up. */
  const stickToBottomRef = useRef(true)

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  const refreshSessionUsage = useCallback(async () => {
    const sid = sessionIdRef.current
    if (!sid) return
    try {
      setUsageNote('')
      const u = await fetchSessionUsage(sid)
      setSessionUsage(u)
    } catch (e) {
      setUsageNote(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const handleMessagesScroll = useCallback(() => {
    const el = messagesScrollRef.current
    if (!el) return
    stickToBottomRef.current = distanceFromBottom(el) <= BOTTOM_SLACK_PX
  }, [])

  useLayoutEffect(() => {
    const el = messagesScrollRef.current
    if (!el || !stickToBottomRef.current) return
    el.scrollTop = el.scrollHeight
  }, [messages, streamingText, toolHint])

  useEffect(() => {
    const el = messagesScrollRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      if (!stickToBottomRef.current) return
      el.scrollTop = el.scrollHeight
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const handleGatewayEvent = useCallback(
    (evt: GatewayJsonEvent) => {
      const rid = typeof evt.request_id === 'string' ? evt.request_id : ''
      switch (evt.type) {
        case 'AssistantDelta': {
          const d = typeof evt.delta === 'string' ? evt.delta : ''
          if (!rid) break
          streamBufRef.current += d
          setStreamingText(streamBufRef.current)
          break
        }
        case 'ToolCallStarted': {
          const tool = typeof evt.tool === 'string' ? evt.tool : 'tool'
          const label = typeof evt.label === 'string' ? evt.label : undefined
          const args = evt.args && typeof evt.args === 'object' ? (evt.args as Record<string, unknown>) : undefined
          setToolHint(null)
          setMessages((m) => [
            ...m,
            {
              id: `ts-${rid}-${tool}-${Date.now()}`,
              role: 'tool',
              content: buildToolStartedChatBody(tool, label, args),
            },
          ])
          break
        }
        case 'ToolCallResult': {
          const tool = typeof evt.tool === 'string' ? evt.tool : 'tool'
          const resultRaw = typeof evt.result === 'string' ? evt.result : ''
          const preview = resultRaw.length > 2000 ? `${resultRaw.slice(0, 2000)}…` : resultRaw
          const err = typeof evt.error === 'string' && evt.error ? evt.error : undefined
          setToolHint(null)
          setMessages((m) => [
            ...m,
            {
              id: `tr-${rid}-${tool}-${Date.now()}`,
              role: 'tool',
              content: buildToolResultChatBody(tool, err, preview),
            },
          ])
          break
        }
        case 'TurnComplete': {
          setToolHint(null)
          setActiveRequestId(null)
          void refreshSessionUsage()
          {
            const text = streamBufRef.current
            streamBufRef.current = ''
            setStreamingText('')
            if (text.trim()) {
              setMessages((m) => [...m, { id: `a-${rid || newRequestId()}`, role: 'assistant', content: text }])
            }
          }
          break
        }
        case 'Error': {
          const err = typeof evt.error === 'string' ? evt.error : 'Unknown error'
          setToolHint(null)
          setActiveRequestId(null)
          streamBufRef.current = ''
          setStreamingText('')
          setMessages((m) => [...m, { id: `e-${newRequestId()}`, role: 'system', content: err }])
          void refreshSessionUsage()
          break
        }
        case 'Thinking': {
          setToolHint('Thinking…')
          break
        }
        default:
          break
      }
    },
    [refreshSessionUsage],
  )

  useEffect(() => {
    if (!sessionId) return

    const ac = new AbortController()
    streamAbortRef.current = ac
    let cancelled = false

    ;(async () => {
      try {
        const res = await openEventsStream(sessionId, ac.signal)
        if (!res.ok) {
          const t = await res.text()
          throw new Error(`events stream: ${res.status} ${t}`)
        }
        await consumeSseJson(res, handleGatewayEvent, ac.signal)
      } catch (e) {
        if (cancelled || ac.signal.aborted) return
        setStatus('error')
        setStatusNote(e instanceof Error ? e.message : String(e))
      }
    })()

    return () => {
      cancelled = true
      ac.abort()
      streamAbortRef.current = null
    }
  }, [sessionId, handleGatewayEvent])

  useEffect(() => {
    if (!sessionId || status !== 'connected') return
    void refreshSessionUsage()
  }, [sessionId, status, refreshSessionUsage])

  const connect = async () => {
    stickToBottomRef.current = true
    setStatus('connecting')
    setStatusNote('')
    setMessages([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(null)
    setSessionUsage(null)
    setUsageNote('')
    try {
      const { session_id } = await createSession()
      setSessionId(session_id)
      setStatus('connected')
    } catch (e) {
      setSessionId(null)
      setStatus('error')
      setStatusNote(e instanceof Error ? e.message : String(e))
    }
  }

  const disconnect = () => {
    stickToBottomRef.current = true
    streamAbortRef.current?.abort()
    setSessionId(null)
    setStatus('disconnected')
    setStatusNote('')
    setMessages([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(null)
    setToolHint(null)
    setSessionUsage(null)
    setUsageNote('')
  }

  const send = async () => {
    const text = draft.trim()
    const sid = sessionId
    if (!text || !sid || status !== 'connected') return

    const rid = newRequestId()
    setDraft('')
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(rid)
    setToolHint(null)
    setMessages((m) => [...m, { id: `u-${rid}`, role: 'user', content: text }])

    try {
      await postReply(sid, rid, text)
    } catch (e) {
      setActiveRequestId(null)
      streamBufRef.current = ''
      setStreamingText('')
      setMessages((m) => [
        ...m,
        {
          id: `e-${rid}`,
          role: 'system',
          content: e instanceof Error ? e.message : String(e),
        },
      ])
    }
  }

  const busy = activeRequestId !== null

  return (
    <div className="app">
      <section className="panel controls" aria-label="Connection and session usage">
        <div className="row">
          {status !== 'connected' ? (
            <button type="button" className="btn primary" onClick={connect} disabled={status === 'connecting'}>
              {status === 'connecting' ? 'Connecting…' : 'New session'}
            </button>
          ) : (
            <button type="button" className="btn" onClick={disconnect} disabled={busy}>
              End session
            </button>
          )}
          <span className={`pill ${status}`} aria-live="polite">
            {status}
            {sessionId ? ` · ${sessionId.slice(0, 8)}…` : ''}
          </span>
          {status === 'connected' && sessionId ? (
            <button type="button" className="btn" onClick={() => void refreshSessionUsage()} disabled={busy}>
              Refresh usage
            </button>
          ) : null}
        </div>
        {status === 'connected' && sessionId ? (
          <div className="usage-row">
            <ContextUsageRing
              lastPromptTokens={sessionUsage?.last_prompt_tokens ?? 0}
              contextWindowTokens={sessionUsage?.context_window_tokens ?? DEFAULT_CONTEXT_WINDOW}
            />
            <div className="usage-strip" aria-label="Session token usage">
              {sessionUsage ? (
                <>
                  <span className="usage-item">
                    <span className="usage-label">Turns</span>
                    <span className="usage-value">{sessionUsage.turns}</span>
                  </span>
                  <span className="usage-sep" aria-hidden>
                    ·
                  </span>
                  <span className="usage-item">
                    <span className="usage-label">In</span>
                    <span className="usage-value">{formatTokens(sessionUsage.input_tokens)}</span>
                  </span>
                  <span className="usage-sep" aria-hidden>
                    ·
                  </span>
                  <span className="usage-item">
                    <span className="usage-label">Out</span>
                    <span className="usage-value">{formatTokens(sessionUsage.output_tokens)}</span>
                  </span>
                </>
              ) : (
                <span className="muted">Usage appears after your first completed reply.</span>
              )}
            </div>
          </div>
        ) : null}
        {usageNote ? <p className="error usage-error">{usageNote}</p> : null}
        {statusNote ? <p className="error">{statusNote}</p> : null}
      </section>

      <main className="panel chat" aria-label="Chat">
        <div
          ref={messagesScrollRef}
          className="messages"
          role="log"
          aria-relevant="additions"
          onScroll={handleMessagesScroll}
        >
          {messages.map((m) => (
            <article key={m.id} className={`bubble ${m.role}`}>
              <div className="bubble-meta">{m.role === 'tool' ? 'tool call' : m.role}</div>
              <div className="bubble-body">{m.content}</div>
            </article>
          ))}
          {(streamingText || toolHint) && (
            <article className="bubble assistant streaming" aria-busy={busy}>
              <div className="bubble-meta">assistant</div>
              <div className="bubble-body">
                {toolHint ? <div className="hint">{toolHint}</div> : null}
                {streamingText ? <div className="stream">{streamingText}</div> : null}
              </div>
            </article>
          )}
        </div>

        <div className="composer">
          <textarea
            className="textarea"
            rows={3}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
            placeholder="Message… (Enter to send, Shift+Enter newline)"
            disabled={status !== 'connected' || busy}
          />
          <button
            type="button"
            className="btn primary send"
            onClick={() => void send()}
            disabled={status !== 'connected' || busy || !draft.trim()}
          >
            Send
          </button>
        </div>
      </main>
    </div>
  )
}
