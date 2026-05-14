/** Browser client for MonkeyBot v2 SSE gateway (see `monkeybot.gateway.sse.routes`). */

export const GATEWAY_BASE = 'http://localhost:8787'

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
  /** Prompt tokens from the provider for the latest completed turn (approx. current context pressure). */
  last_prompt_tokens: number
  /** Cap from gateway `MODEL_CONTEXT_WINDOW` (default 1_000_000). */
  context_window_tokens: number
}

export type UsageSnapshot = {
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_usd: number
  duration_ms: number
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
