import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import ImageBlockBubble from './blocks/ImageBlockBubble'
import ElicitationForm from './blocks/ElicitationForm'
import FrontendToolHandler from './blocks/FrontendToolHandler'
import RedactedThinkingPlaceholder from './blocks/RedactedThinkingPlaceholder'
import SystemNotificationToast from './blocks/SystemNotificationToast'
import ThinkingPanel from './blocks/ThinkingPanel'
import ToolConfirmationModal from './blocks/ToolConfirmationModal'
import ToolInvocationCard, { MAX_RESULT_CHARS } from './blocks/ToolInvocationCard'
import {
  consumeSseJson,
  createSession,
  fetchSessionUsage,
  openEventsStream,
  postCancel,
  postReply,
  type GatewayJsonEvent,
  type SessionUsageResponse,
} from './gatewayClient'
import WorkspaceBrowser from './WorkspaceBrowser'
import type { ToastItem } from './blocks/SystemNotificationToast'

export type PendingWidget =
  | {
      kind: 'toolConfirmation'
      tool_call_id: string
      tool_name: string
      arguments: Record<string, unknown>
      prompt?: string
      request_id?: string
    }
  | {
      kind: 'elicitation'
      id: string
      message: string
      /** Raw schema object from the gateway payload — keys as emitted (expect JSON-Schema-ish map). */
      requested_schema: Record<string, unknown>
      request_id?: string
    }
  | {
      kind: 'frontendTool'
      tool_call_id: string
      name: string
      args: Record<string, unknown>
      request_id?: string
    }

export type ChatFeedItem =
  | {
      kind: 'userText'
      id: string
      text: string
    }
  | {
      kind: 'assistantText'
      id: string
      text: string
    }
  | {
      kind: 'toolInvocation'
      id: string
      request_id: string
      tool: string
      label?: string
      args?: Record<string, unknown>
      phase: 'running' | 'complete'
      error?: string
      resultRaw?: string
      resultTruncated?: boolean
    }
  | {
      kind: 'systemNotice'
      id: string
      text: string
    }
  | {
      kind: 'image'
      id: string
      request_id?: string
      mime_type: string
      data: string
    }
  | {
      kind: 'thinking'
      id: string
      request_id: string
      text: string
      phase: 'streaming' | 'complete'
      signature?: string
    }
  | {
      kind: 'redactedThinking'
      id: string
      request_id?: string
      data: string
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

function formatTokens(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

const DEFAULT_CONTEXT_WINDOW = 1_000_000

function IconRefresh() {
  return (
    <svg
      className="btn-icon-svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.85"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 3" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 21" />
      <path d="M3 21v-5h5" />
    </svg>
  )
}

function IconX() {
  return (
    <svg
      className="btn-icon-svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  )
}

function IconPlus() {
  return (
    <svg
      className="btn-icon-svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}

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
  const r = 11
  const stroke = 2.35
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
  const [feed, setFeed] = useState<ChatFeedItem[]>([])
  const [pendingWidgets, setPendingWidgets] = useState<PendingWidget[]>([])
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const [draft, setDraft] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null)
  const [toolHint, setToolHint] = useState<string | null>(null)

  const [sessionUsage, setSessionUsage] = useState<SessionUsageResponse | null>(null)
  const [usageNote, setUsageNote] = useState('')
  const [systemPromptSnap, setSystemPromptSnap] = useState<{
    requestId: string
    innerTurn: number
    text: string
  } | null>(null)
  const [rightTab, setRightTab] = useState<'prompt' | 'workspace'>('prompt')

  const streamAbortRef = useRef<AbortController | null>(null)
  const streamBufRef = useRef('')
  const messagesScrollRef = useRef<HTMLDivElement | null>(null)
  const chatSurfaceRef = useRef<HTMLDivElement | null>(null)
  const overlayRef = useRef<HTMLDivElement | null>(null)
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
  }, [feed, streamingText, toolHint])

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

  useLayoutEffect(() => {
    const surface = chatSurfaceRef.current
    const overlay = overlayRef.current
    if (!surface || !overlay) return

    const syncOverlayPad = () => {
      const h = Math.ceil(overlay.getBoundingClientRect().height)
      surface.style.setProperty('--chat-overlay-pad', `${h}px`)
      const msg = messagesScrollRef.current
      if (msg && stickToBottomRef.current) {
        msg.scrollTop = msg.scrollHeight
      }
    }

    syncOverlayPad()
    const ro = new ResizeObserver(syncOverlayPad)
    ro.observe(overlay)
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
          const labelRaw = typeof evt.label === 'string' ? evt.label : ''
          const label = labelRaw.trim() ? labelRaw : undefined
          const args = evt.args && typeof evt.args === 'object' ? (evt.args as Record<string, unknown>) : undefined
          setToolHint(null)
          setFeed((m) => [
            ...m,
            {
              id: `ti-${rid}-${tool}-${Date.now()}`,
              kind: 'toolInvocation',
              request_id: rid,
              tool,
              label,
              args,
              phase: 'running',
            },
          ])
          break
        }
        case 'ToolCallResult': {
          const tool = typeof evt.tool === 'string' ? evt.tool : 'tool'
          const resultFull = typeof evt.result === 'string' ? evt.result : ''
          const resultTruncated = resultFull.length > MAX_RESULT_CHARS
          const resultRaw = resultTruncated ? resultFull.slice(0, MAX_RESULT_CHARS) : resultFull
          const err = typeof evt.error === 'string' && evt.error ? evt.error : undefined
          setToolHint(null)
          setFeed((m) => {
            let idx = -1
            for (let i = m.length - 1; i >= 0; i--) {
              const x = m[i]
              if (
                x.kind === 'toolInvocation' &&
                x.request_id === rid &&
                x.tool === tool &&
                x.phase === 'running'
              ) {
                idx = i
                break
              }
            }
            if (idx === -1) {
              return [
                ...m,
                {
                  id: `ti-orphan-${rid}-${tool}-${Date.now()}`,
                  kind: 'toolInvocation',
                  request_id: rid,
                  tool,
                  phase: 'complete',
                  error: err,
                  resultRaw,
                  resultTruncated,
                },
              ]
            }
            const cur = m[idx] as Extract<ChatFeedItem, { kind: 'toolInvocation' }>
            const next: Extract<ChatFeedItem, { kind: 'toolInvocation' }> = {
              ...cur,
              phase: 'complete',
              error: err,
              resultRaw,
              resultTruncated,
            }
            return m.map((x, i) => (i === idx ? next : x))
          })
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
              setFeed((m) => [...m, { id: `a-${rid || newRequestId()}`, kind: 'assistantText', text }])
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
          setFeed((m) => [...m, { id: `e-${newRequestId()}`, kind: 'systemNotice', text: err }])
          void refreshSessionUsage()
          break
        }
        case 'Thinking': {
          setToolHint('Thinking…')
          break
        }
        case 'SystemPromptSnapshot': {
          const tid = typeof evt.request_id === 'string' ? evt.request_id : ''
          const it = evt.inner_turn
          const innerTurn = typeof it === 'number' && Number.isFinite(it) ? it : 0
          const body = typeof evt.text === 'string' ? evt.text : ''
          setSystemPromptSnap({ requestId: tid, innerTurn, text: body })
          break
        }
        case 'ImageBlock': {
          const mime = typeof evt.mime_type === 'string' ? evt.mime_type : 'application/octet-stream'
          const b64 = typeof evt.data === 'string' ? evt.data : ''
          if (!b64) break
          setFeed((f) => [
            ...f,
            {
              kind: 'image',
              id: `img-${typeof evt.request_id === 'string' ? evt.request_id : newRequestId()}`,
              request_id: typeof evt.request_id === 'string' ? evt.request_id : undefined,
              mime_type: mime,
              data: b64,
            },
          ])
          break
        }
        case 'ThinkingBlockDelta': {
          const thRid = typeof evt.request_id === 'string' ? evt.request_id : ''
          if (!thRid) break
          const delta = typeof evt.text === 'string' ? evt.text : ''
          const sig = typeof evt.signature === 'string' ? evt.signature : undefined
          setFeed((f) => {
            const i = f.findIndex((x) => x.kind === 'thinking' && x.request_id === thRid && x.phase === 'streaming')
            if (i === -1) {
              return [
                ...f,
                {
                  kind: 'thinking',
                  id: `th-${thRid}`,
                  request_id: thRid,
                  text: delta,
                  phase: 'streaming',
                  signature: sig,
                },
              ]
            }
            const cur = f[i] as Extract<ChatFeedItem, { kind: 'thinking' }>
            const next = { ...cur, text: cur.text + delta, signature: sig ?? cur.signature }
            return f.map((x, j) => (j === i ? next : x))
          })
          break
        }
        case 'ThinkingBlockComplete': {
          const thRid = typeof evt.request_id === 'string' ? evt.request_id : ''
          if (!thRid) break
          const sig = typeof evt.signature === 'string' ? evt.signature : ''
          setFeed((f) => {
            const i = f.findIndex((x) => x.kind === 'thinking' && x.request_id === thRid)
            if (i === -1) {
              return [
                ...f,
                {
                  kind: 'thinking',
                  id: `th-${thRid}`,
                  request_id: thRid,
                  text: '',
                  phase: 'complete',
                  signature: sig,
                },
              ]
            }
            const cur = f[i] as Extract<ChatFeedItem, { kind: 'thinking' }>
            const next: Extract<ChatFeedItem, { kind: 'thinking' }> = {
              ...cur,
              phase: 'complete',
              signature: sig || cur.signature,
            }
            return f.map((x, j) => (j === i ? next : x))
          })
          break
        }
        case 'RedactedThinkingBlock': {
          const b64 = typeof evt.data === 'string' ? evt.data : ''
          setFeed((f) => [
            ...f,
            {
              kind: 'redactedThinking',
              id: `rth-${typeof evt.request_id === 'string' ? evt.request_id : newRequestId()}`,
              request_id: typeof evt.request_id === 'string' ? evt.request_id : undefined,
              data: b64,
            },
          ])
          break
        }
        case 'ToolConfirmationRequest': {
          const callId = typeof evt.tool_call_id === 'string' ? evt.tool_call_id : ''
          if (!callId) break
          setPendingWidgets((w) => [
            ...w,
            {
              kind: 'toolConfirmation',
              tool_call_id: callId,
              tool_name: typeof evt.tool_name === 'string' ? evt.tool_name : 'tool',
              arguments: evt.arguments && typeof evt.arguments === 'object' ? (evt.arguments as Record<string, unknown>) : {},
              prompt: typeof evt.prompt === 'string' ? evt.prompt : undefined,
              request_id: typeof evt.request_id === 'string' ? evt.request_id : undefined,
            },
          ])
          break
        }
        case 'ActionRequiredEvent': {
          if (evt.action_type !== 'elicitation') {
            setFeed((f) => [
              ...f,
              {
                kind: 'systemNotice',
                id: `ar-${newRequestId()}`,
                text: `[ActionRequired:${String(evt.action_type)}] ${JSON.stringify(evt.payload ?? {})}`,
              },
            ])
            break
          }
          const eid = typeof evt.id === 'string' ? evt.id : ''
          if (!eid) break
          const payload = evt.payload && typeof evt.payload === 'object' ? (evt.payload as Record<string, unknown>) : {}
          const message = typeof payload.message === 'string' ? payload.message : ''
          const schemaRaw = payload.requested_schema ?? payload.requestedSchema
          const requested_schema =
            schemaRaw && typeof schemaRaw === 'object' ? (schemaRaw as Record<string, unknown>) : {}
          setPendingWidgets((w) => [
            ...w,
            {
              kind: 'elicitation',
              id: eid,
              message,
              requested_schema,
              request_id: typeof evt.request_id === 'string' ? evt.request_id : undefined,
            },
          ])
          break
        }
        case 'FrontendToolRequest': {
          const callId = typeof evt.tool_call_id === 'string' ? evt.tool_call_id : ''
          const name = typeof evt.name === 'string' ? evt.name : ''
          if (!callId || !name) break
          setPendingWidgets((w) => [
            ...w,
            {
              kind: 'frontendTool',
              tool_call_id: callId,
              name,
              args: evt.args && typeof evt.args === 'object' ? (evt.args as Record<string, unknown>) : {},
              request_id: typeof evt.request_id === 'string' ? evt.request_id : undefined,
            },
          ])
          break
        }
        case 'SystemNotificationEvent': {
          const msg = typeof evt.msg === 'string' ? evt.msg : ''
          if (!msg) break
          setToasts((t) => [
            ...t,
            {
              id: `sn-${typeof evt.request_id === 'string' ? evt.request_id : newRequestId()}`,
              notification_type: typeof evt.notification_type === 'string' ? evt.notification_type : 'inlineMessage',
              msg,
            },
          ])
          break
        }
        case 'ContextSummarizing':
        case 'ContextSummarized':
          break
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
      } finally {
        if (!cancelled) {
          setPendingWidgets([])
        }
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
    setFeed([])
    setPendingWidgets([])
    setToasts([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(null)
    setSessionUsage(null)
    setUsageNote('')
    setSystemPromptSnap(null)
    setRightTab('prompt')
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
    setFeed([])
    setPendingWidgets([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(null)
    setToolHint(null)
    setSessionUsage(null)
    setUsageNote('')
    setSystemPromptSnap(null)
    setRightTab('prompt')
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
    setFeed((m) => [...m, { id: `u-${rid}`, kind: 'userText', text }])

    try {
      await postReply(sid, rid, text)
    } catch (e) {
      setActiveRequestId(null)
      streamBufRef.current = ''
      setStreamingText('')
      setFeed((m) => [
        ...m,
        {
          id: `e-${rid}`,
          kind: 'systemNotice',
          text: e instanceof Error ? e.message : String(e),
        },
      ])
    }
  }

  const stopTurn = async () => {
    const sid = sessionId
    const rid = activeRequestId
    if (!sid || !rid || status !== 'connected') return
    try {
      await postCancel(sid, rid)
      setPendingWidgets([])
    } catch (e) {
      setFeed((m) => [
        ...m,
        {
          id: `e-${newRequestId()}`,
          kind: 'systemNotice',
          text: e instanceof Error ? e.message : String(e),
        },
      ])
    }
  }

  const busy = activeRequestId !== null

  const dismissToast = useCallback((id: string) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const dequeuePending = useCallback(() => {
    setPendingWidgets((w) => w.slice(1))
  }, [])

  const headPending = pendingWidgets[0]

  return (
    <div className="app app--split">
      <main className="panel chat" aria-label="Chat">
        <div className="chat-surface" ref={chatSurfaceRef}>
          <div
            ref={messagesScrollRef}
            className="messages"
            role="log"
            aria-relevant="additions"
            onScroll={handleMessagesScroll}
          >
            {feed.map((item) => {
              if (item.kind === 'userText') {
                return (
                  <article key={item.id} className="bubble user">
                    <div className="bubble-meta">user</div>
                    <div className="bubble-body">{item.text}</div>
                  </article>
                )
              }
              if (item.kind === 'assistantText') {
                return (
                  <article key={item.id} className="bubble assistant">
                    <div className="bubble-meta">assistant</div>
                    <div className="bubble-body">{item.text}</div>
                  </article>
                )
              }
              if (item.kind === 'toolInvocation') {
                return (
                  <article key={item.id} className="bubble tool">
                    <div className="bubble-meta">tool</div>
                    <div className="bubble-body bubble-body--tool-card">
                      <ToolInvocationCard
                        tool={item.tool}
                        label={item.label}
                        args={item.args}
                        phase={item.phase}
                        error={item.error}
                        resultRaw={item.resultRaw}
                        resultTruncated={item.resultTruncated}
                      />
                    </div>
                  </article>
                )
              }
              if (item.kind === 'systemNotice') {
                return (
                  <article key={item.id} className="bubble system">
                    <div className="bubble-meta">system</div>
                    <div className="bubble-body">{item.text}</div>
                  </article>
                )
              }
              if (item.kind === 'image') {
                return (
                  <article key={item.id} className="bubble assistant assistant-image-inline">
                    <div className="bubble-meta">assistant</div>
                    <div className="bubble-body">
                      <ImageBlockBubble mime_type={item.mime_type} data={item.data} />
                    </div>
                  </article>
                )
              }
              if (item.kind === 'thinking') {
                return (
                  <article key={item.id} className="bubble assistant thinking-feed-item">
                    <div className="bubble-meta">thinking</div>
                    <div className="bubble-body">
                      <ThinkingPanel
                        request_id={item.request_id}
                        text={item.text}
                        phase={item.phase}
                        signature={item.signature}
                      />
                    </div>
                  </article>
                )
              }
              if (item.kind === 'redactedThinking') {
                return (
                  <article key={item.id} className="bubble assistant">
                    <div className="bubble-meta">thinking</div>
                    <div className="bubble-body">
                      <RedactedThinkingPlaceholder data={item.data} />
                    </div>
                  </article>
                )
              }
              return null
            })}
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

          <SystemNotificationToast toasts={toasts} onDismiss={dismissToast} />

          {sessionId && headPending ? (
            <div className="pending-widgets-root">
              {headPending.kind === 'toolConfirmation' ? (
                <ToolConfirmationModal
                  sessionId={sessionId}
                  widget={headPending}
                  onResolved={dequeuePending}
                  onError={() => {}}
                />
              ) : headPending.kind === 'elicitation' ? (
                <ElicitationForm
                  sessionId={sessionId}
                  widget={headPending}
                  onResolved={dequeuePending}
                  onError={() => {}}
                />
              ) : (
                <FrontendToolHandler
                  sessionId={sessionId}
                  widget={headPending}
                  onResolved={dequeuePending}
                  onError={() => {}}
                />
              )}
            </div>
          ) : null}

          <div ref={overlayRef} className="chat-controls-overlay" role="region" aria-label="Connection and session usage">
            <div className="chat-controls-bar">
              <div className="chat-controls-leading row">
                <span className={`pill ${status}`} aria-live="polite">
                  {status}
                  {sessionId ? ` · ${sessionId.slice(0, 8)}…` : ''}
                </span>
                {status === 'connected' && sessionId ? (
                  <div className="usage-row usage-row--toolbar">
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
              </div>
              <div className="chat-controls-trailing row">
                {status === 'connected' && sessionId ? (
                  <>
                    <button
                      type="button"
                      className="btn btn-icon"
                      aria-label="Refresh usage"
                      title="Refresh usage"
                      onClick={() => void refreshSessionUsage()}
                      disabled={busy}
                    >
                      <IconRefresh />
                    </button>
                    <button
                      type="button"
                      className="btn btn-icon btn-icon-danger"
                      aria-label="End session"
                      title="End session"
                      onClick={disconnect}
                      disabled={busy}
                    >
                      <IconX />
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="btn btn-icon primary"
                    onClick={connect}
                    disabled={status === 'connecting'}
                    aria-busy={status === 'connecting'}
                    aria-label={status === 'connecting' ? 'Connecting…' : 'New session'}
                    title={status === 'connecting' ? 'Connecting…' : 'New session'}
                  >
                    <IconPlus />
                  </button>
                )}
              </div>
            </div>
            {usageNote || statusNote ? (
              <div className="chat-controls-notes">
                {usageNote ? <p className="error usage-error">{usageNote}</p> : null}
                {statusNote ? <p className="error">{statusNote}</p> : null}
              </div>
            ) : null}
          </div>
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
          <div className="composer-actions">
            {busy ? (
              <button type="button" className="btn stop" onClick={() => void stopTurn()}>
                Stop
              </button>
            ) : null}
            <button
              type="button"
              className="btn primary send"
              onClick={() => void send()}
              disabled={status !== 'connected' || busy || !draft.trim()}
            >
              Send
            </button>
          </div>
        </div>
      </main>

      <aside className="panel right-side-panel" aria-label="Session context">
        <div className="right-panel-tabs" role="tablist" aria-label="Side panel">
          <button
            type="button"
            role="tab"
            className="right-panel-tab"
            aria-selected={rightTab === 'prompt'}
            onClick={() => setRightTab('prompt')}
          >
            System prompt
          </button>
          <button
            type="button"
            role="tab"
            className="right-panel-tab"
            aria-selected={rightTab === 'workspace'}
            onClick={() => setRightTab('workspace')}
          >
            Workspace
          </button>
        </div>
        {rightTab === 'prompt' ? (
          <div className="right-panel-pane system-prompt-pane" role="tabpanel">
            <h2 className="panel-heading">System prompt</h2>
            <p className="system-prompt-meta">
              {systemPromptSnap ? (
                <>
                  Request <span title={systemPromptSnap.requestId}>{systemPromptSnap.requestId.slice(0, 10)}…</span>
                  {' · '}
                  inner turn {systemPromptSnap.innerTurn}
                  {' · '}
                  {systemPromptSnap.text.length.toLocaleString()} chars
                </>
              ) : (
                <>
                  Updates on each model call (after curation). Connect and send a message to see the composed prompt.
                </>
              )}
            </p>
            <div className="system-prompt-scroll" tabIndex={0}>
              {systemPromptSnap ? (
                <pre className="system-prompt-pre">{systemPromptSnap.text}</pre>
              ) : (
                <p className="system-prompt-empty">No snapshot yet.</p>
              )}
            </div>
          </div>
        ) : (
          <div className="right-panel-pane workspace-pane" role="tabpanel" aria-label="Workspace files">
            <WorkspaceBrowser />
          </div>
        )}
      </aside>
    </div>
  )
}
