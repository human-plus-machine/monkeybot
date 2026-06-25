import type { ChatFeedItem } from './chatTypes'

/** Commit or drop the in-flight assistant prose block for a request (before tools / turn end). */
export function finalizeStreamingAssistant(
  feed: ChatFeedItem[],
  requestId: string,
): ChatFeedItem[] {
  return feed.flatMap((item) => {
    if (
      item.kind === 'assistantText' &&
      item.phase === 'streaming' &&
      item.request_id === requestId
    ) {
      if (!item.text.trim()) return []
      return [{ ...item, phase: 'complete' as const }]
    }
    return [item]
  })
}

/** Append assistant prose deltas into a single streaming feed row (interleaved with tools). */
export function upsertStreamingAssistant(
  feed: ChatFeedItem[],
  requestId: string,
  text: string,
): ChatFeedItem[] {
  const i = feed.findIndex(
    (x) =>
      x.kind === 'assistantText' &&
      x.phase === 'streaming' &&
      x.request_id === requestId,
  )
  if (i === -1) {
    return [
      ...feed,
      {
        kind: 'assistantText',
        id: `a-stream-${requestId}-${Date.now()}`,
        request_id: requestId,
        text,
        phase: 'streaming',
      },
    ]
  }
  const cur = feed[i] as Extract<ChatFeedItem, { kind: 'assistantText' }>
  const next: Extract<ChatFeedItem, { kind: 'assistantText' }> = { ...cur, text }
  return feed.map((x, j) => (j === i ? next : x))
}

/** Merge pending ref text into feed, then commit streaming prose (same SSE chunk safe). */
export function flushStreamingAssistant(
  feed: ChatFeedItem[],
  requestId: string,
  pendingText: string,
): ChatFeedItem[] {
  const merged = pendingText.trim()
    ? upsertStreamingAssistant(feed, requestId, pendingText)
    : feed
  return finalizeStreamingAssistant(merged, requestId)
}
