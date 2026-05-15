export type UnknownEventFallbackProps = {
  title: string
  /** Pretty-printed JSON string of the unknown / unsupported payload. */
  bodyText: string
  onCopy?: () => void
}

export default function UnknownEventFallback({ title, bodyText, onCopy }: UnknownEventFallbackProps) {
  return (
    <div className="unknown-event-fallback" data-testid="unknown-event-fallback">
      <div className="bubble-meta">{title}</div>
      <pre className="unknown-event-pre">{bodyText}</pre>
      <button type="button" className="btn" onClick={onCopy ?? (() => void navigator.clipboard?.writeText(bodyText))}>
        Copy
      </button>
    </div>
  )
}
