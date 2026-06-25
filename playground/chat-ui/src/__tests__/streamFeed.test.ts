import { describe, expect, it } from 'vitest'
import type { ChatFeedItem } from '../chatTypes'
import { finalizeStreamingAssistant, flushStreamingAssistant, upsertStreamingAssistant } from '../streamFeed'

describe('streamFeed', () => {
  it('upsertStreamingAssistant appends and updates one streaming row per request', () => {
    const rid = 'req-1'
    let feed: ChatFeedItem[] = []
    feed = upsertStreamingAssistant(feed, rid, 'Hello')
    feed = upsertStreamingAssistant(feed, rid, 'Hello world')
    expect(feed).toHaveLength(1)
    expect(feed[0]).toMatchObject({
      kind: 'assistantText',
      request_id: rid,
      text: 'Hello world',
      phase: 'streaming',
    })
  })

  it('finalizeStreamingAssistant commits prose and drops empty streaming rows', () => {
    const rid = 'req-1'
    const feed: ChatFeedItem[] = [
      {
        kind: 'assistantText',
        id: 'a1',
        request_id: rid,
        text: 'Before tool',
        phase: 'streaming',
      },
      {
        kind: 'assistantText',
        id: 'a2',
        request_id: rid,
        text: '   ',
        phase: 'streaming',
      },
    ]
    const out = finalizeStreamingAssistant(feed, rid)
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ text: 'Before tool', phase: 'complete' })
  })

  it('flushStreamingAssistant merges pending ref text before finalize', () => {
    const rid = 'req-1'
    const out = flushStreamingAssistant([], rid, 'Buffered prose')
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ text: 'Buffered prose', phase: 'complete' })
  })

  it('interleaves prose segments with tools when finalized before each tool', () => {
    const rid = 'req-1'
    let feed: ChatFeedItem[] = []
    feed = upsertStreamingAssistant(feed, rid, 'Step one')
    feed = finalizeStreamingAssistant(feed, rid)
    feed = [
      ...feed,
      {
        kind: 'toolInvocation',
        id: 't1',
        request_id: rid,
        tool: 'list_skills',
        phase: 'running',
      },
    ]
    feed = upsertStreamingAssistant(feed, rid, 'Step two')
    feed = finalizeStreamingAssistant(feed, rid)
    feed = [
      ...feed,
      {
        kind: 'toolInvocation',
        id: 't2',
        request_id: rid,
        tool: 'read_file',
        phase: 'running',
      },
    ]
    expect(feed.map((x) => (x.kind === 'toolInvocation' ? x.tool : x.text))).toEqual([
      'Step one',
      'list_skills',
      'Step two',
      'read_file',
    ])
  })
})
