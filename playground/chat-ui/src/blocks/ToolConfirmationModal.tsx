import { useState } from 'react'
import type { PendingWidget } from '../App.tsx'
import { postToolConfirmation } from '../gatewayClient'

export type ToolConfirmationModalProps = {
  sessionId: string
  widget: Extract<PendingWidget, { kind: 'toolConfirmation' }>
  onResolved: () => void
  onError: (message: string) => void
}

/**
 * Approve / deny gateway tool execution. Successful POST (2xx) removes the widget via `onResolved`.
 */
export default function ToolConfirmationModal({
  widget,
  sessionId,
  onResolved,
  onError,
}: ToolConfirmationModalProps) {
  const [inlineError, setInlineError] = useState('')
  const [busy, setBusy] = useState(false)
  const [reason, setReason] = useState('')

  const argsPretty = JSON.stringify(widget.arguments ?? {}, null, 2)

  const send = async (approved: boolean) => {
    setInlineError('')
    setBusy(true)
    try {
      const body: { approved: boolean; reason?: string } = { approved }
      if (!approved && reason.trim()) body.reason = reason.trim()
      await postToolConfirmation(sessionId, widget.tool_call_id, body)
      onResolved()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setInlineError(msg)
      onError(msg)
    } finally {
      setBusy(false)
    }
  }

  const handleApprove = () => void send(true)

  const handleDeny = () => void send(false)

  return (
    <div className="chat-modal-overlay" role="presentation" data-testid="tool-confirmation-modal">
      <div className="chat-modal-shell" role="dialog" aria-modal="true" aria-label="Tool confirmation">
        <h3 className="chat-modal-heading">{widget.tool_name}</h3>
        <pre className="chat-modal-pre">{argsPretty}</pre>
        {widget.prompt ? <p className="chat-modal-prompt">{widget.prompt}</p> : null}
        <label className="elicitation-field denial-reason-label">
          Reason for denial (optional)
          <textarea
            className="textarea denial-reason-textarea"
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
          />
        </label>
        <div className="chat-modal-actions row">
          <button type="button" className="btn primary" onClick={handleApprove} disabled={busy}>
            Approve
          </button>
          <button type="button" className="btn" onClick={handleDeny} disabled={busy}>
            Deny
          </button>
        </div>
        {inlineError ? (
          <div className="error chat-modal-inline-error" role="alert">
            {inlineError}
          </div>
        ) : null}
      </div>
    </div>
  )
}
