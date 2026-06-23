import {
  memo,
  useState,
  useCallback,
  useRef,
  useEffect,
  useMemo,
  type ReactNode,
  type Ref,
} from 'react'
import { cn } from '../lib/cn'

export type ChatStatus = 'ready' | 'streaming' | 'submitted' | 'idle'

export type MessagePart =
  | { type: 'text'; text: string }
  | { type: 'error'; title?: string; message: string }
  | {
      type: 'files'
      files: {
        id: string
        filename: string
        mimeType: string
        frozen?: boolean
      }[]
    }
  | { type: 'slot'; slotId: string }

export type AgentMessage = {
  id: string
  role: 'user' | 'assistant'
  parts: MessagePart[]
}

export type AttachedImage = {
  id: string
  filename: string
  url: string
  size?: number
}

export type AttachedFile = {
  id: string
  filename: string
  size?: number
}

export type AgentChatProps = {
  messages: AgentMessage[]
  onSend?: (message: { role: 'user'; content: string }) => void
  onStop?: () => void
  status?: ChatStatus
  error?: { message: string; title?: string }
  attachments?: {
    onAttach?: () => void
    images?: AttachedImage[]
    files?: AttachedFile[]
    onRemoveImage?: (id: string) => void
    onRemoveFile?: (id: string) => void
  }
  slots?: Record<string, ReactNode>
  messageListFooter?: ReactNode
  messageListRef?: Ref<HTMLDivElement>
  onMessageListScroll?: () => void
  inputDisabled?: boolean
  inputPlaceholder?: string
  className?: string
}

const SendIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <line x1="12" y1="19" x2="12" y2="5" />
    <polyline points="5 12 12 5 19 12" />
  </svg>
)

const StopIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
    <rect x="6" y="6" width="12" height="12" rx="1" />
  </svg>
)

const PaperclipIcon = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
  </svg>
)

const XIcon = ({ size = 12 }: { size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
)

const FileIcon = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
)

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] px-3.5 py-2 rounded-2xl bg-neutral-100 dark:bg-neutral-800 text-sm text-neutral-900 dark:text-neutral-100 whitespace-pre-wrap break-words">
        {text}
      </div>
    </div>
  )
}

function AssistantText({ text }: { text: string }) {
  return (
    <div className="flex justify-start">
      <div className="max-w-[90%] text-sm leading-relaxed text-neutral-700 dark:text-neutral-300 whitespace-pre-wrap break-words">
        {text}
      </div>
    </div>
  )
}

function ErrorBubble({
  title = 'Something went wrong',
  message,
}: {
  title?: string
  message: string
}) {
  return (
    <div className="flex justify-start">
      <div className="border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm rounded-[8px]">
        <div className="font-medium text-neutral-900 dark:text-neutral-100">{title}</div>
        <div className="mt-0.5 text-neutral-500 dark:text-neutral-400">{message}</div>
      </div>
    </div>
  )
}

function HistoryFileChip({
  filename,
  frozen,
}: {
  filename: string
  frozen?: boolean
}) {
  return (
    <div
      className={cn(
        'inline-flex items-center gap-2 px-2 py-1.5 rounded-md bg-neutral-100 dark:bg-neutral-800',
        frozen && 'ring-1 ring-blue-500/40',
      )}
      data-testid="attachment-chip"
      data-frozen={frozen ? 'true' : undefined}
    >
      <span className="text-neutral-500 dark:text-neutral-400">
        <FileIcon />
      </span>
      <span className="text-xs font-medium truncate text-neutral-900 dark:text-neutral-100 max-w-[180px]">
        {filename}
      </span>
    </div>
  )
}

function MessageList({
  messages,
  slots,
  messageListFooter,
  messageListRef,
  onMessageListScroll,
}: {
  messages: AgentMessage[]
  slots?: Record<string, ReactNode>
  messageListFooter?: ReactNode
  messageListRef?: Ref<HTMLDivElement>
  onMessageListScroll?: () => void
}) {
  return (
    <div
      ref={messageListRef}
      className="flex-1 min-h-0 overflow-y-auto px-4 py-6"
      role="log"
      aria-relevant="additions"
      onScroll={onMessageListScroll}
      data-testid="agent-chat-message-list"
    >
      <div className="mx-auto w-full max-w-full flex flex-col gap-4">
        {messages.map((m) => (
          <div key={m.id} className="flex flex-col gap-2">
            {m.parts.map((part, i) => {
              if (part.type === 'slot') {
                return <div key={`${m.id}-slot-${i}`}>{slots?.[part.slotId] ?? null}</div>
              }
              if (part.type === 'files') {
                return (
                  <div
                    key={`${m.id}-files-${i}`}
                    className="flex justify-end"
                    data-testid="user-attachments"
                  >
                    <div className="flex flex-wrap gap-1.5 max-w-[80%]">
                      {part.files.map((f) => (
                        <HistoryFileChip
                          key={f.id}
                          filename={f.filename}
                          frozen={f.frozen}
                        />
                      ))}
                    </div>
                  </div>
                )
              }
              if (part.type === 'error') {
                return (
                  <ErrorBubble key={`${m.id}-err-${i}`} title={part.title} message={part.message} />
                )
              }
              if (m.role === 'user') {
                return <UserBubble key={`${m.id}-text-${i}`} text={part.text} />
              }
              return <AssistantText key={`${m.id}-text-${i}`} text={part.text} />
            })}
          </div>
        ))}
        {messageListFooter}
      </div>
    </div>
  )
}

function ImageChip({ url, onRemove }: { url: string; onRemove?: () => void }) {
  return (
    <div className="relative w-12 h-12 rounded-md overflow-hidden bg-neutral-100 dark:bg-neutral-800 group">
      <img src={url} alt="" className="w-full h-full object-cover" />
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label="Remove image"
          className="absolute top-0.5 right-0.5 inline-flex items-center justify-center w-4 h-4 rounded-full bg-neutral-900/70 text-white opacity-0 group-hover:opacity-100 transition-opacity"
        >
          <XIcon size={10} />
        </button>
      )}
    </div>
  )
}

function FileChip({
  filename,
  size,
  onRemove,
}: {
  filename: string
  size?: number
  onRemove?: () => void
}) {
  const sizeText =
    size === undefined
      ? null
      : size < 1024
        ? `${size} B`
        : size < 1024 * 1024
          ? `${(size / 1024).toFixed(1)} KB`
          : `${(size / (1024 * 1024)).toFixed(1)} MB`
  return (
    <div
      className="inline-flex items-center gap-2 px-2 py-1.5 rounded-md bg-neutral-100 dark:bg-neutral-800 group"
      data-testid="attachment-chip"
    >
      <span className="text-neutral-500 dark:text-neutral-400">
        <FileIcon />
      </span>
      <div className="flex flex-col min-w-0">
        <span className="text-xs font-medium truncate text-neutral-900 dark:text-neutral-100 max-w-[140px]">
          {filename}
        </span>
        {sizeText && (
          <span className="text-[10px] text-neutral-500 dark:text-neutral-400">{sizeText}</span>
        )}
      </div>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label="Remove file"
          className="inline-flex items-center justify-center w-5 h-5 rounded-full text-neutral-500 dark:text-neutral-400 hover:bg-neutral-200 dark:hover:bg-neutral-700 opacity-0 group-hover:opacity-100 transition-opacity"
        >
          <XIcon />
        </button>
      )}
    </div>
  )
}

function InputBar({
  onSend,
  onStop,
  status = 'ready',
  placeholder = 'Send a message...',
  attachments,
  className,
  value: controlledValue,
  onChange,
  disabled,
}: {
  onSend?: (m: { role: 'user'; content: string }) => void
  onStop?: () => void
  status?: ChatStatus
  placeholder?: string
  attachments?: AgentChatProps['attachments']
  className?: string
  value?: string
  onChange?: (v: string) => void
  disabled?: boolean
}) {
  const [internal, setInternal] = useState('')
  const isControlled = controlledValue !== undefined
  const input = isControlled ? controlledValue : internal
  const setInput = useCallback(
    (v: string) => {
      if (isControlled) onChange?.(v)
      else setInternal(v)
    },
    [isControlled, onChange],
  )
  const ref = useRef<HTMLTextAreaElement>(null)
  const isStreaming = status === 'streaming' || status === 'submitted'
  const hasInput = input.trim().length > 0
  const images = attachments?.images ?? []
  const files = attachments?.files ?? []
  const hasContext = images.length > 0 || files.length > 0
  const canSend = hasInput || hasContext

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = '0'
    const next = Math.min(el.scrollHeight, 120)
    el.style.height = `${next}px`
    el.style.overflowY = el.scrollHeight > 120 ? 'auto' : 'hidden'
  }, [input])

  const submit = useCallback(() => {
    const trimmed = input.trim()
    if ((!trimmed && !hasContext) || isStreaming || disabled) return
    onSend?.({ role: 'user', content: trimmed })
    setInput('')
  }, [input, hasContext, isStreaming, disabled, onSend, setInput])

  return (
    <div className={cn('shrink-0 px-3 pb-3 w-full', className)}>
      <div className="mx-auto w-full max-w-full">
        <div
          className="relative cursor-text rounded-[16px] bg-white dark:bg-neutral-900 shadow-sm ring-1 ring-neutral-200 dark:ring-neutral-800"
          onClick={(e) => {
            if (
              e.target === e.currentTarget ||
              !(e.target as HTMLElement).closest('button, textarea')
            ) {
              ref.current?.focus()
            }
          }}
        >
          <div
            className={cn(
              'grid transition-[grid-template-rows] duration-200 ease-out',
              hasContext ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
            )}
          >
            <div className="overflow-hidden">
              {hasContext && (
                <div
                  className="flex flex-wrap items-center gap-1.5 px-2.5 pt-2.5 pb-0.5"
                  data-testid="composer-pending-attachments"
                >
                  {images.map((img) => (
                    <ImageChip
                      key={img.id}
                      url={img.url}
                      onRemove={
                        attachments?.onRemoveImage
                          ? () => attachments.onRemoveImage!(img.id)
                          : undefined
                      }
                    />
                  ))}
                  {files.map((f) => (
                    <FileChip
                      key={f.id}
                      filename={f.filename}
                      size={f.size}
                      onRemove={
                        attachments?.onRemoveFile
                          ? () => attachments.onRemoveFile!(f.id)
                          : undefined
                      }
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="pt-3 pb-0 pr-3 pl-3.5 min-h-[44px]">
            <textarea
              ref={ref}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  submit()
                }
              }}
              placeholder={placeholder}
              disabled={disabled}
              rows={1}
              className={cn(
                'w-full resize-none bg-transparent border-0 outline-none text-[14px] leading-[1.6] text-neutral-900 dark:text-neutral-100 placeholder:text-neutral-400 dark:placeholder:text-neutral-500 overflow-hidden',
                disabled && 'opacity-50 cursor-not-allowed',
              )}
            />
          </div>
          <div className="flex items-center justify-between gap-3 px-2 pt-1 pb-2">
            <div className="flex items-center gap-1 min-w-0">
              {attachments?.onAttach && (
                <button
                  type="button"
                  onClick={attachments.onAttach}
                  aria-label="Attach"
                  className="inline-flex items-center justify-center w-8 h-8 rounded-full text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors disabled:opacity-40"
                  disabled={disabled}
                >
                  <PaperclipIcon />
                </button>
              )}
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                aria-label={isStreaming ? 'Stop' : 'Send'}
                onClick={() => {
                  if (isStreaming) onStop?.()
                  else if (canSend) submit()
                }}
                disabled={!isStreaming && (!canSend || disabled)}
                className={cn(
                  'inline-flex items-center justify-center w-8 h-8 rounded-full transition-all duration-150',
                  isStreaming || (canSend && !disabled)
                    ? 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900'
                    : 'bg-neutral-200 text-neutral-400 dark:bg-neutral-800 dark:text-neutral-600',
                )}
              >
                {isStreaming ? <StopIcon /> : <SendIcon />}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export const AgentChat = memo(function AgentChat({
  messages,
  onSend,
  onStop,
  status = 'ready',
  error,
  attachments,
  slots,
  messageListFooter,
  messageListRef,
  onMessageListScroll,
  inputDisabled,
  inputPlaceholder,
  className,
}: AgentChatProps) {
  const [draft, setDraft] = useState('')

  const messagesWithError: AgentMessage[] = useMemo(() => {
    if (!error) return messages
    return [
      ...messages,
      {
        id: `agent-chat-error-${error.title ?? ''}-${error.message}`,
        role: 'assistant' as const,
        parts: [
          {
            type: 'error' as const,
            title: error.title ?? 'Request failed',
            message: error.message,
          },
        ],
      },
    ]
  }, [messages, error])

  const inputBarNode: ReactNode = (
    <InputBar
      onSend={onSend}
      onStop={onStop}
      status={status}
      attachments={attachments}
      value={draft}
      onChange={setDraft}
      disabled={inputDisabled}
      placeholder={
        inputPlaceholder ??
        (inputDisabled ? 'Starting session…' : 'Message… (Enter to send, Shift+Enter newline)')
      }
    />
  )

  return (
    <div className={cn('flex flex-col h-full min-h-0', className)}>
      <MessageList
        messages={messagesWithError}
        slots={slots}
        messageListFooter={messageListFooter}
        messageListRef={messageListRef}
        onMessageListScroll={onMessageListScroll}
      />
      {inputBarNode}
    </div>
  )
})

export default AgentChat
