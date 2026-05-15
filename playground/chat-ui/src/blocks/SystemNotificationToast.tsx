import { useEffect } from 'react'

export type ToastItem = {
  id: string
  notification_type: string
  msg: string
}

export type SystemNotificationToastProps = {
  toasts: ToastItem[]
  onDismiss: (id: string) => void
}

const AUTO_MS = 5_000

/**
 * Stacking toast notifications with optional auto-dismiss (not for `creditsExhausted`).
 */
export default function SystemNotificationToast({ toasts, onDismiss }: SystemNotificationToastProps) {
  return (
    <div className="toast-stack" aria-live="polite" aria-relevant="additions text">
      {toasts.map((t) => (
        <ToastRow key={t.id} toast={t} onDismiss={() => onDismiss(t.id)} />
      ))}
    </div>
  )
}

function ToastRow({ toast, onDismiss }: { toast: ToastItem; onDismiss: () => void }) {
  useEffect(() => {
    if (toast.notification_type === 'creditsExhausted') return undefined
    const id = window.setTimeout(() => onDismiss(), AUTO_MS)
    return () => window.clearTimeout(id)
  }, [onDismiss, toast.id, toast.notification_type])

  return (
    <div className={`toast-bubble toast-bubble-${toast.notification_type}`} role="status">
      <span>{toast.msg}</span>
      <button type="button" className="btn btn-icon toast-dismiss" aria-label="Dismiss" onClick={onDismiss}>
        ×
      </button>
    </div>
  )
}
