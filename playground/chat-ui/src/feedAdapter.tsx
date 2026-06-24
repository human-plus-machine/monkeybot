import type { ReactNode } from 'react'
import ImageBlockBubble from './blocks/ImageBlockBubble'
import RedactedThinkingPlaceholder from './blocks/RedactedThinkingPlaceholder'
import ThinkingPanel from './blocks/ThinkingPanel'
import ToolInvocationCard from './blocks/ToolInvocationCard'
import type { AgentMessage, MessagePart } from './components/AgentChat'
import type { ChatFeedItem } from './chatTypes'
import type { ChatHistoryMessage } from './gatewayClient'

/** Map persisted chat history into feed items for session resume.

 * Intentionally text-only: the history API returns ``role`` + ``text`` per message.
 * Tool calls, thinking blocks, and image attachments from the live SSE stream are
 * not stored in that shape and are omitted here by design (not a rendering bug).
 */
export function historyMessagesToFeed(messages: ChatHistoryMessage[]): ChatFeedItem[] {
  const items: ChatFeedItem[] = []
  messages.forEach((msg, idx) => {
    if (msg.role === 'user') {
      items.push({
        kind: 'userText',
        id: `hist-u-${idx}`,
        text: msg.text,
      })
      return
    }
    items.push({
      kind: 'assistantText',
      id: `hist-a-${idx}`,
      text: msg.text,
    })
  })
  return items
}

export function feedToAgentMessages(feed: ChatFeedItem[]): AgentMessage[] {
  const messages: AgentMessage[] = []

  for (const item of feed) {
    switch (item.kind) {
      case 'userText': {
        const parts: MessagePart[] = []
        if (item.text.trim()) {
          parts.push({ type: 'text', text: item.text })
        }
        if (item.attachments?.length) {
          parts.push({
            type: 'files',
            files: item.attachments.map((a) => ({
              id: a.attachment_id,
              filename: a.filename,
              mimeType: a.mime_type,
              frozen: a.frozen,
            })),
          })
        }
        messages.push({ id: item.id, role: 'user', parts })
        break
      }
      case 'assistantText':
        messages.push({
          id: item.id,
          role: 'assistant',
          parts: [{ type: 'text', text: item.text }],
        })
        break
      case 'systemNotice':
        messages.push({
          id: item.id,
          role: 'assistant',
          parts: [{ type: 'error', title: 'System', message: item.text }],
        })
        break
      case 'toolInvocation':
      case 'image':
      case 'thinking':
      case 'redactedThinking':
        messages.push({
          id: item.id,
          role: 'assistant',
          parts: [{ type: 'slot', slotId: item.id }],
        })
        break
      default:
        break
    }
  }

  return messages
}

export function buildFeedSlots(feed: ChatFeedItem[]): Record<string, ReactNode> {
  const slots: Record<string, ReactNode> = {}

  for (const item of feed) {
    if (item.kind === 'toolInvocation') {
      slots[item.id] = (
        <div className="flex justify-start">
          <div className="max-w-[90%] rounded-lg border border-neutral-200 dark:border-neutral-800 bg-neutral-50 dark:bg-neutral-900/50 p-3 text-sm">
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
        </div>
      )
      continue
    }
    if (item.kind === 'image') {
      slots[item.id] = (
        <div className="flex justify-start">
          <ImageBlockBubble mime_type={item.mime_type} data={item.data} />
        </div>
      )
      continue
    }
    if (item.kind === 'thinking') {
      slots[item.id] = (
        <div className="flex justify-start max-w-[90%]">
          <ThinkingPanel
            request_id={item.request_id}
            text={item.text}
            phase={item.phase}
            signature={item.signature}
          />
        </div>
      )
      continue
    }
    if (item.kind === 'redactedThinking') {
      slots[item.id] = (
        <div className="flex justify-start max-w-[90%]">
          <RedactedThinkingPlaceholder data={item.data} />
        </div>
      )
    }
  }

  return slots
}
