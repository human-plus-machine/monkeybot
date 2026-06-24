import { describe, expect, it } from 'vitest'
import { historyMessagesToFeed } from '../feedAdapter'
import type { ChatHistoryMessage } from '../gatewayClient'

describe('historyMessagesToFeed', () => {
  it('maps user and assistant text messages', () => {
    const messages: ChatHistoryMessage[] = [
      { role: 'user', text: 'hello' },
      { role: 'assistant', text: 'hi there' },
    ]
    const feed = historyMessagesToFeed(messages)
    expect(feed).toEqual([
      { kind: 'userText', id: 'hist-u-0', text: 'hello' },
      { kind: 'assistantText', id: 'hist-a-1', text: 'hi there' },
    ])
  })

  it('returns text-only feed items on session resume (no tool/thinking/image slots)', () => {
    // History API returns role+text only; richer SSE blocks are intentionally omitted.
    const messages: ChatHistoryMessage[] = [
      { role: 'user', text: 'run search' },
      { role: 'assistant', text: 'Here is the answer.' },
    ]
    const feed = historyMessagesToFeed(messages)
    expect(feed.every((item) => item.kind === 'userText' || item.kind === 'assistantText')).toBe(
      true,
    )
    expect(feed.some((item) => item.kind === 'toolInvocation')).toBe(false)
    expect(feed.some((item) => item.kind === 'thinking')).toBe(false)
    expect(feed.some((item) => item.kind === 'image')).toBe(false)
  })
})
