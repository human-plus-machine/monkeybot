export type UserAttachmentView = {
  attachment_id: string
  filename: string
  mime_type: string
  description?: string
  frozen?: boolean
}

export type ChatFeedItem =
  | {
      kind: 'userText'
      id: string
      request_id?: string
      text: string
      attachments?: UserAttachmentView[]
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

export type PendingComposerAttachment = {
  attachment_id: string
  filename: string
  mime_type: string
  size_bytes?: number
  preview_url?: string
}
