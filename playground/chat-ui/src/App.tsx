import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import ElicitationForm from './blocks/ElicitationForm'
import FrontendToolHandler from './blocks/FrontendToolHandler'
import SystemNotificationToast from './blocks/SystemNotificationToast'
import ToolConfirmationModal from './blocks/ToolConfirmationModal'
import { MAX_RESULT_CHARS } from './blocks/ToolInvocationCard'
import AgentChat, { type ChatStatus } from './components/AgentChat'
import type { ChatFeedItem, PendingComposerAttachment } from './chatTypes'
import { buildFeedSlots, feedToAgentMessages } from './feedAdapter'
import {
  consumeSseJson,
  createSession,
  fetchSessionUsage,
  openEventsStream,
  postAttachment,
  postCancel,
  postReply,
  type ContentBlockWire,
  type GatewayJsonEvent,
  type SessionUsageResponse,
} from './gatewayClient'
import EvalsPanel from './EvalsPanel'
import ObservabilityPanel from './ObservabilityPanel'
import WorkspaceBrowser from './WorkspaceBrowser'
import type { ToastItem } from './blocks/SystemNotificationToast'

export type { ChatFeedItem, UserAttachmentView } from './chatTypes'

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

const MAX_ATTACHMENTS_PER_REPLY = 5

function revokeAttachmentPreview(a: PendingComposerAttachment): void {
  if (a.preview_url) URL.revokeObjectURL(a.preview_url)
}

function buildReplyContent(
  text: string,
  attachments: PendingComposerAttachment[],
): ContentBlockWire[] {
  const blocks: ContentBlockWire[] = []
  const trimmed = text.trim()
  if (trimmed) {
    blocks.push({ type: 'text', text: trimmed })
  }
  for (const a of attachments) {
    blocks.push({
      type: 'attachmentRef',
      attachmentId: a.attachment_id,
      mimeType: a.mime_type,
      metadata: { filename: a.filename },
    })
  }
  return blocks
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

function formatUsd(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '$0.00'
  return `$${n.toFixed(4)}`
}

const DEFAULT_CONTEXT_WINDOW = 200_000

// Playground dev defaults — provider API model ids (some preview). Each option needs
// matching credentials in playground/agent/.env (OPENAI_API_KEY, GCP ADC, etc.).
const MODEL_OPTIONS = [
  { label: 'OpenAI', provider: 'openai', model: 'gpt-5' },
  { label: 'Vertex Gemini', provider: 'gemini', model: 'gemini-3-flash-preview' },
  { label: 'Anthropic (Vertex)', provider: 'vertex-claude', model: 'claude-haiku-4-5' },
] as const

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
  estimatedPromptTokens,
  lastPromptTokens,
  contextWindowTokens,
  summarizationThresholdTokens,
}: {
  estimatedPromptTokens: number
  lastPromptTokens: number
  contextWindowTokens: number
  summarizationThresholdTokens: number
}) {
  const cap = Math.max(1, contextWindowTokens)
  const ringNumerator =
    estimatedPromptTokens > 0 ? estimatedPromptTokens : lastPromptTokens
  const thresh = Math.max(
    1,
    summarizationThresholdTokens > 0
      ? summarizationThresholdTokens
      : Math.floor(cap * 0.85),
  )
  const fracUsed = Math.min(1, Math.max(0, ringNumerator / cap))
  const pctUsed = Math.round(fracUsed * 100)
  const r = 11
  const stroke = 2.35
  const c = 2 * Math.PI * r
  const dash = fracUsed * c
  const strokeColor =
    ringNumerator >= thresh ? 'var(--danger)' : ringNumerator >= thresh * 0.75 ? '#ca8a04' : '#4ade80'
  const detail = `${formatTokens(ringNumerator)} / ${formatTokens(cap)} · ${pctUsed}%`
  const tip = `Context usage: ${formatTokens(ringNumerator)} of ${formatTokens(cap)} pre-flight prompt tokens (${pctUsed}%). Sync summarization typically runs near ${formatTokens(thresh)} (same ratio as the agent loop).`

  return (
    <div className="context-ring-wrap" tabIndex={0} aria-label={tip}>
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
        {pctUsed}%
      </span>
      <div className="context-ring-tooltip" aria-hidden>
        <div className="context-ring-tooltip-title">Context usage</div>
        <div className="context-ring-tooltip-detail">{detail}</div>
      </div>
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
  const [modelIdx, setModelIdx] = useState(0)
  const [feed, setFeed] = useState<ChatFeedItem[]>([])
  const [pendingWidgets, setPendingWidgets] = useState<PendingWidget[]>([])
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const [pendingAttachments, setPendingAttachments] = useState<PendingComposerAttachment[]>([])
  const [attachBusy, setAttachBusy] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
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
  const [rightTab, setRightTab] = useState<'prompt' | 'workspace' | 'observability' | 'evals'>('prompt')
  const [lastTraceId, setLastTraceId] = useState<string | null>(null)

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
          const traceId =
            typeof evt.trace_id === 'string' && evt.trace_id.length > 0 ? evt.trace_id : null
          setLastTraceId(traceId)
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
        case 'AttachmentDescriptor': {
          const attId = typeof evt.attachment_id === 'string' ? evt.attachment_id : ''
          if (!rid || !attId) break
          const filename = typeof evt.filename === 'string' ? evt.filename : attId
          const mime = typeof evt.mime_type === 'string' ? evt.mime_type : 'application/octet-stream'
          const desc = typeof evt.description === 'string' ? evt.description : ''
          setFeed((f) =>
            f.map((item) => {
              if (item.kind !== 'userText' || item.request_id !== rid || !item.attachments?.length) {
                return item
              }
              const nextAtt = item.attachments.map((a) =>
                a.attachment_id === attId
                  ? { ...a, filename, mime_type: mime, description: desc, frozen: true }
                  : a,
              )
              return { ...item, attachments: nextAtt }
            }),
          )
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
    setPendingAttachments([])
    setPendingWidgets([])
    setToasts([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(null)
    setSessionUsage(null)
    setUsageNote('')
    setSystemPromptSnap(null)
    setLastTraceId(null)
    setRightTab('prompt')
    try {
      const opt = MODEL_OPTIONS[modelIdx]
      const { session_id } = await createSession(undefined, {
        model_provider: opt.provider,
        model_name: opt.model,
      })
      setSessionId(session_id)
      setStatus('connected')
    } catch (e) {
      setSessionId(null)
      const msg = e instanceof Error ? e.message : String(e)
      if (msg.includes('MODEL_UNAVAILABLE')) {
        setStatus('disconnected')
        setStatusNote('Selected model unavailable — check credentials or pick another.')
      } else {
        setStatus('error')
        setStatusNote(msg)
      }
    }
  }

  const disconnect = () => {
    stickToBottomRef.current = true
    streamAbortRef.current?.abort()
    setSessionId(null)
    setStatus('disconnected')
    setStatusNote('')
    setFeed([])
    pendingAttachments.forEach(revokeAttachmentPreview)
    setPendingAttachments([])
    setPendingWidgets([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(null)
    setToolHint(null)
    setSessionUsage(null)
    setUsageNote('')
    setSystemPromptSnap(null)
    setLastTraceId(null)
    setRightTab('prompt')
  }

  const attachFiles = async (files: FileList | null) => {
    const sid = sessionId
    if (!sid || status !== 'connected' || !files?.length || busy || attachBusy) return
    setAttachBusy(true)
    try {
      const availableSlots = MAX_ATTACHMENTS_PER_REPLY - pendingAttachments.length
      const selected = Array.from(files).slice(0, Math.max(0, availableSlots))
      const added = await Promise.all(
        selected.map(async (file): Promise<PendingComposerAttachment> => {
          const uploaded = await postAttachment(sid, file)
          let preview_url: string | undefined
          if (file.type.startsWith('image/')) {
            try {
              preview_url = URL.createObjectURL(file)
            } catch {
              preview_url = undefined
            }
          }
          return {
            attachment_id: uploaded.attachment_id,
            filename: uploaded.filename || file.name,
            mime_type: uploaded.mime_type,
            size_bytes: file.size,
            preview_url,
          }
        }),
      )
      if (added.length) {
        setPendingAttachments((prev) => [...prev, ...added].slice(0, MAX_ATTACHMENTS_PER_REPLY))
      }
    } catch (e) {
      setFeed((m) => [
        ...m,
        {
          id: `e-${newRequestId()}`,
          kind: 'systemNotice',
          text: e instanceof Error ? e.message : String(e),
        },
      ])
    } finally {
      setAttachBusy(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const send = async (text: string) => {
    const trimmed = text.trim()
    const sid = sessionId
    if ((!trimmed && pendingAttachments.length === 0) || !sid || status !== 'connected') return

    const rid = newRequestId()
    const attachments = [...pendingAttachments]
    attachments.forEach(revokeAttachmentPreview)
    setPendingAttachments([])
    streamBufRef.current = ''
    setStreamingText('')
    setActiveRequestId(rid)
    setToolHint(null)
    setFeed((m) => [
      ...m,
      {
        id: `u-${rid}`,
        kind: 'userText',
        request_id: rid,
        text: trimmed,
        attachments: attachments.length
          ? attachments.map((a) => ({
              attachment_id: a.attachment_id,
              filename: a.filename,
              mime_type: a.mime_type,
            }))
          : undefined,
      },
    ])

    try {
      await postReply(sid, rid, buildReplyContent(trimmed, attachments))
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

  const agentMessages = useMemo(() => feedToAgentMessages(feed), [feed])
  const feedSlots = useMemo(() => buildFeedSlots(feed), [feed])

  const chatStatus: ChatStatus =
    status !== 'connected' || !sessionId
      ? 'idle'
      : busy
        ? streamingText || toolHint
          ? 'streaming'
          : 'submitted'
        : 'ready'

  const composerImages = useMemo(
    () =>
      pendingAttachments
        .filter((a) => a.mime_type.startsWith('image/') && a.preview_url)
        .map((a) => ({
          id: a.attachment_id,
          filename: a.filename,
          url: a.preview_url!,
          size: a.size_bytes,
        })),
    [pendingAttachments],
  )

  const composerFiles = useMemo(
    () =>
      pendingAttachments
        .filter((a) => !a.mime_type.startsWith('image/') || !a.preview_url)
        .map((a) => ({
          id: a.attachment_id,
          filename: a.filename,
          size: a.size_bytes,
        })),
    [pendingAttachments],
  )

  const removePendingAttachment = useCallback((id: string) => {
    setPendingAttachments((prev) => {
      const target = prev.find((a) => a.attachment_id === id)
      if (target) revokeAttachmentPreview(target)
      return prev.filter((a) => a.attachment_id !== id)
    })
  }, [])

  const messageListFooter =
    streamingText || toolHint ? (
      <div className="flex justify-start" aria-busy={busy}>
        <div className="max-w-[90%] text-sm leading-relaxed text-neutral-700 dark:text-neutral-300 whitespace-pre-wrap break-words">
          {toolHint ? <div className="mb-1 text-xs italic text-neutral-500">{toolHint}</div> : null}
          {streamingText}
        </div>
      </div>
    ) : null

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
          <AgentChat
            className="h-full"
            messages={agentMessages}
            slots={feedSlots}
            messageListRef={messagesScrollRef}
            onMessageListScroll={handleMessagesScroll}
            messageListFooter={messageListFooter}
            status={chatStatus}
            onSend={({ content }) => void send(content)}
            onStop={() => void stopTurn()}
            inputDisabled={status !== 'connected' || attachBusy}
            attachments={{
              onAttach:
                status === 'connected' &&
                !busy &&
                !attachBusy &&
                pendingAttachments.length < MAX_ATTACHMENTS_PER_REPLY
                  ? () => fileInputRef.current?.click()
                  : undefined,
              images: composerImages,
              files: composerFiles,
              onRemoveImage: removePendingAttachment,
              onRemoveFile: removePendingAttachment,
            }}
          />

          <input
            ref={fileInputRef}
            type="file"
            className="composer-file-input"
            accept="image/jpeg,image/png,image/gif,image/webp,application/pdf"
            multiple
            aria-hidden
            tabIndex={-1}
            onChange={(e) => void attachFiles(e.target.files)}
          />

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
                      estimatedPromptTokens={sessionUsage?.estimated_prompt_tokens ?? 0}
                      lastPromptTokens={sessionUsage?.last_prompt_tokens ?? 0}
                      contextWindowTokens={sessionUsage?.context_window_tokens ?? DEFAULT_CONTEXT_WINDOW}
                      summarizationThresholdTokens={
                        sessionUsage?.summarization_threshold_tokens ??
                        Math.max(1, Math.floor(DEFAULT_CONTEXT_WINDOW * 0.85))
                      }
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
                          <span className="usage-sep" aria-hidden>
                            ·
                          </span>
                          <span className="usage-item">
                            <span className="usage-label">Cost</span>
                            <span className="usage-value">{formatUsd(sessionUsage.cost_usd)}</span>
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
                <select
                  className="btn"
                  aria-label="Model"
                  value={modelIdx}
                  onChange={(e) => setModelIdx(Number(e.target.value))}
                  disabled={status === 'connecting' || status === 'connected'}
                >
                  {MODEL_OPTIONS.map((opt, idx) => (
                    <option key={opt.label} value={idx}>
                      {opt.label}
                    </option>
                  ))}
                </select>
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
          <button
            type="button"
            role="tab"
            className="right-panel-tab"
            aria-selected={rightTab === 'observability'}
            onClick={() => setRightTab('observability')}
          >
            Observability
          </button>
          <button
            type="button"
            role="tab"
            className="right-panel-tab"
            aria-selected={rightTab === 'evals'}
            onClick={() => setRightTab('evals')}
          >
            Evals
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
        ) : rightTab === 'workspace' ? (
          <div className="right-panel-pane workspace-pane" role="tabpanel" aria-label="Workspace files">
            <WorkspaceBrowser />
          </div>
        ) : rightTab === 'observability' ? (
          <div className="right-panel-pane observability-pane-wrap" role="tabpanel" aria-label="Observability">
            <ObservabilityPanel
              lastTraceId={lastTraceId}
              observabilityEnabled={lastTraceId != null}
            />
          </div>
        ) : (
          <div className="right-panel-pane evals-pane-wrap" role="tabpanel" aria-label="Quality evals">
            <EvalsPanel />
          </div>
        )}
      </aside>
    </div>
  )
}
