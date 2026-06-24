import { useEffect, useRef } from 'react'
import type { ChatHistoryThread } from './gatewayClient'

type ChatHistoryPanelProps = {
  open: boolean
  loading: boolean
  error: string
  threads: ChatHistoryThread[]
  activeSessionId: string | null
  containerRef: React.RefObject<HTMLElement | null>
  onClose: () => void
  onRefresh: () => void
  onSelect: (sessionId: string) => void
}

function formatWhen(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return ''
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    }).format(new Date(ms))
  } catch {
    return new Date(ms).toLocaleString()
  }
}

export default function ChatHistoryPanel({
  open,
  loading,
  error,
  threads,
  activeSessionId,
  containerRef,
  onClose,
  onRefresh,
  onSelect,
}: ChatHistoryPanelProps) {
  const panelRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDocClick = (ev: MouseEvent) => {
      const el = containerRef.current
      if (!el) return
      if (ev.target instanceof Node && !el.contains(ev.target)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open, onClose, containerRef])

  if (!open) return null

  return (
    <div ref={panelRef} className="chat-history-panel" role="dialog" aria-label="Chat history">
      <div className="chat-history-header">
        <h3 className="chat-history-title">Previous chats</h3>
        <button
          type="button"
          className="btn btn-icon chat-history-refresh"
          aria-label="Refresh chat history"
          title="Refresh"
          onClick={onRefresh}
          disabled={loading}
        >
          ↻
        </button>
      </div>
      {error ? <p className="error chat-history-error">{error}</p> : null}
      {loading && threads.length === 0 ? (
        <p className="muted chat-history-empty">Loading…</p>
      ) : null}
      {!loading && !error && threads.length === 0 ? (
        <p className="muted chat-history-empty">No saved chats yet. Start a conversation first.</p>
      ) : null}
      <ul className="chat-history-list">
        {threads.map((thread) => {
          const active = activeSessionId === thread.session_id
          return (
            <li key={thread.session_id}>
              <button
                type="button"
                className={`chat-history-item${active ? ' chat-history-item-active' : ''}`}
                onClick={() => onSelect(thread.session_id)}
                disabled={loading}
              >
                <span className="chat-history-item-preview">{thread.preview}</span>
                <span className="chat-history-item-meta">
                  <span>{formatWhen(thread.last_message_at)}</span>
                  <span aria-hidden> · </span>
                  <span>{thread.message_count} msgs</span>
                  <span aria-hidden> · </span>
                  <span className="chat-history-item-id">{thread.session_id.slice(0, 8)}…</span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
