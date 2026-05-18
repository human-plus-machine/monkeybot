/** Browser client for MonkeyBot v2 SSE gateway (see `monkeybot.gateway.sse.routes`). */

function resolveGatewayBase(): string {
  if (import.meta.env.DEV) {
    return '/__mb_gateway'
  }
  const fromEnv = (import.meta.env.VITE_GATEWAY_TARGET as string | undefined)?.trim()
  if (fromEnv) return fromEnv.replace(/\/$/, '')
  return 'http://127.0.0.1:8787'
}

export const GATEWAY_BASE = resolveGatewayBase()

export type GatewayJsonEvent = {
  type: string
  request_id?: string
  chat_request_id?: string
  delta?: string
  error?: string
  tool?: string
  label?: string
  result?: string
  args?: Record<string, unknown>
  usage?: Record<string, unknown>
  trace_id?: string
  inner_turn?: number
  text?: string
  mime_type?: string
  data?: string | null
  signature?: string | null
  tool_call_id?: string
  tool_name?: string
  arguments?: Record<string, unknown>
  prompt?: string
  action_type?: string
  id?: string
  payload?: Record<string, unknown>
  name?: string
  notification_type?: string
  msg?: string
}

export type SessionUsageResponse = {
  session_id: string
  turns: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_usd: number
  period_start: number
  period_end: number
  /** Provider input tokens for the latest completed turn. */
  last_prompt_tokens: number
  /** Peak chars/4 estimate for latest turn (Monkeybot summarization heuristic). */
  estimated_prompt_tokens: number
  /** Same threshold as the agent loop before sync summarization (≈85% of window). */
  summarization_threshold_tokens: number
  /** Cap from gateway `MODEL_CONTEXT_WINDOW`. */
  context_window_tokens: number
}

export type UsageSnapshot = {
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_usd: number
  duration_ms: number
}

export type WorkspaceTreeEntry = {
  name: string
  path: string
  kind: 'dir' | 'file'
}

export type WorkspaceTreeResponse = {
  path: string
  entries: WorkspaceTreeEntry[]
}

export type WorkspaceFileResponse = {
  path: string
  content: string
  start_line: number
  end_line: number
  total_lines: number
  truncated: boolean
}

const USAGE_ZERO: UsageSnapshot = {
  input_tokens: 0,
  output_tokens: 0,
  cached_tokens: 0,
  cost_usd: 0,
  duration_ms: 0,
}

/** Parse ``usage`` object from ``TurnComplete`` SSE payloads (or similar). */
export function usageFromUnknown(raw: unknown): UsageSnapshot {
  if (!raw || typeof raw !== 'object') return { ...USAGE_ZERO }
  const o = raw as Record<string, unknown>
  const num = (k: string) => {
    const v = o[k]
    if (typeof v === 'number' && Number.isFinite(v)) return v
    if (typeof v === 'string' && v.trim() !== '') {
      const n = Number(v)
      return Number.isFinite(n) ? n : 0
    }
    return 0
  }
  return {
    input_tokens: Math.max(0, Math.trunc(num('input_tokens'))),
    output_tokens: Math.max(0, Math.trunc(num('output_tokens'))),
    cached_tokens: Math.max(0, Math.trunc(num('cached_tokens'))),
    cost_usd: Math.max(0, num('cost_usd')),
    duration_ms: Math.max(0, Math.trunc(num('duration_ms'))),
  }
}

function parseSseBlocks(buffer: string): { events: GatewayJsonEvent[]; rest: string } {
  const events: GatewayJsonEvent[] = []
  const parts = buffer.split(/\r?\n\r?\n/)
  const rest = parts.pop() ?? ''
  for (const block of parts) {
    const trimmed = block.trim()
    if (!trimmed || trimmed.startsWith(':')) continue
    for (const line of block.split(/\r?\n/)) {
      if (!line.startsWith('data:')) continue
      const raw = line.slice(5).trim()
      if (!raw) continue
      try {
        events.push(JSON.parse(raw) as GatewayJsonEvent)
      } catch {
        /* ignore non-JSON data lines */
      }
    }
  }
  return { events, rest: rest }
}

/**
 * Read an SSE response body and invoke onEvent for each JSON `data:` payload.
 */
export async function consumeSseJson(
  response: Response,
  onEvent: (event: GatewayJsonEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.body) {
    throw new Error('Response has no body')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let carry = ''
  try {
    while (true) {
      if (signal?.aborted) break
      const { done, value } = await reader.read()
      if (done) break
      carry += decoder.decode(value, { stream: true })
      const { events, rest } = parseSseBlocks(carry)
      carry = rest
      for (const e of events) {
        onEvent(e)
      }
    }
  } finally {
    const { events: tail } = parseSseBlocks(`${carry}\n\n`)
    for (const e of tail) {
      onEvent(e)
    }
  }
}

function gatewayErrorDetail(status: number, bodyText: string): string {
  try {
    const j = JSON.parse(bodyText) as { error?: { message?: string } }
    if (j?.error?.message) return `${status}: ${j.error.message}`
  } catch {
    /* ignore */
  }
  return `${status} ${bodyText}`.trim()
}

export async function fetchWorkspaceTree(
  relPath?: string,
  init?: RequestInit,
): Promise<WorkspaceTreeResponse> {
  const q =
    relPath != null && relPath !== '' && relPath !== '.'
      ? `?path=${encodeURIComponent(relPath)}`
      : ''
  const res = await fetch(`${GATEWAY_BASE}/api/playground/workspace/tree${q}`, {
    method: 'GET',
    headers: { ...(init?.headers as Record<string, string>) },
    signal: init?.signal,
  })
  const text = await res.text()
  if (!res.ok) {
    throw new Error(`workspace tree: ${gatewayErrorDetail(res.status, text)}`)
  }
  return JSON.parse(text) as WorkspaceTreeResponse
}

export async function fetchWorkspaceFile(
  path: string,
  opts?: { offset?: number; limit?: number },
  init?: RequestInit,
): Promise<WorkspaceFileResponse> {
  const params = new URLSearchParams({ path })
  if (opts?.offset != null) params.set('offset', String(opts.offset))
  if (opts?.limit != null) params.set('limit', String(opts.limit))
  const res = await fetch(`${GATEWAY_BASE}/api/playground/workspace/file?${params.toString()}`, {
    method: 'GET',
    headers: { ...(init?.headers as Record<string, string>) },
    signal: init?.signal,
  })
  const text = await res.text()
  if (!res.ok) {
    throw new Error(`workspace file: ${gatewayErrorDetail(res.status, text)}`)
  }
  return JSON.parse(text) as WorkspaceFileResponse
}

export async function createSession(
  init?: RequestInit,
): Promise<{ session_id: string; created_at: number }> {
  const res = await fetch(`${GATEWAY_BASE}/sessions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers as Record<string, string>),
    },
    body: JSON.stringify({}),
    signal: init?.signal,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`createSession failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<{ session_id: string; created_at: number }>
}

export async function postReply(
  sessionId: string,
  requestId: string,
  message: string,
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(`${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/reply`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers as Record<string, string>),
    },
    body: JSON.stringify({ request_id: requestId, message }),
    signal: init?.signal,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`reply failed: ${res.status} ${text}`)
  }
}

export async function postCancel(
  sessionId: string,
  requestId: string,
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(`${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers as Record<string, string>),
    },
    body: JSON.stringify({ request_id: requestId }),
    signal: init?.signal,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`cancel failed: ${res.status} ${text}`)
  }
}

export function openEventsStream(sessionId: string, signal: AbortSignal): Promise<Response> {
  return fetch(`${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/events`, {
    method: 'GET',
    headers: {
      Accept: 'text/event-stream',
    },
    signal,
  })
}

export async function fetchSessionUsage(
  sessionId: string,
  init?: RequestInit,
): Promise<SessionUsageResponse> {
  const res = await fetch(`${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/usage`, {
    method: 'GET',
    headers: {
      ...(init?.headers as Record<string, string>),
    },
    signal: init?.signal,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`usage failed: ${res.status} ${text}`)
  }
  return res.json() as Promise<SessionUsageResponse>
}

export async function postToolConfirmation(
  sessionId: string,
  toolCallId: string,
  body: { approved: boolean; reason?: string },
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(
    `${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/tool-confirmations/${encodeURIComponent(toolCallId)}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers as Record<string, string>),
      },
      body: JSON.stringify(body),
      signal: init?.signal,
    },
  )
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`tool confirmation failed: ${res.status} ${text}`)
  }
}

export async function postElicitation(
  sessionId: string,
  id: string,
  body: { user_data: unknown },
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(
    `${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(id)}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers as Record<string, string>),
      },
      body: JSON.stringify(body),
      signal: init?.signal,
    },
  )
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`elicitation failed: ${res.status} ${text}`)
  }
}

export async function postFrontendToolResult(
  sessionId: string,
  toolCallId: string,
  body: { result: unknown[]; is_error: boolean },
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(
    `${GATEWAY_BASE}/sessions/${encodeURIComponent(sessionId)}/frontend-tool-results/${encodeURIComponent(toolCallId)}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers as Record<string, string>),
      },
      body: JSON.stringify(body),
      signal: init?.signal,
    },
  )
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`frontend tool result failed: ${res.status} ${text}`)
  }
}
