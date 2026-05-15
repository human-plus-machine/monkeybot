import { useEffect, useState } from 'react'

export type ThinkingPanelProps = {
  request_id: string
  text: string
  phase: 'streaming' | 'complete'
  signature?: string
}

const SUMMARY_MAX = 100

function firstVisibleLineSummary(raw: string): string {
  const lines = raw.split(/\n/)
  const first = lines.map((l) => l.trim()).find((l) => l.length > 0) ?? ''
  if (first.length <= SUMMARY_MAX) return first
  return first.slice(0, SUMMARY_MAX) + '…'
}

/**
 * Streams full thinking text. On complete, collapses to the first non-empty line (≤100 chars) until clicked.
 */
export default function ThinkingPanel({ request_id, text, phase, signature }: ThinkingPanelProps) {
  const [expanded, setExpanded] = useState(phase === 'streaming')

  useEffect(() => {
    if (phase === 'complete') setExpanded(false)
    if (phase === 'streaming') setExpanded(true)
  }, [phase])

  const summary = firstVisibleLineSummary(text)

  const shell = (
    <>
      {signature ? <div className="thinking-signature-muted">{signature}</div> : null}
      {phase === 'streaming' || expanded ? (
        <pre className="thinking-panel-pre">{text}</pre>
      ) : (
        <pre className="thinking-panel-pre thinking-panel-pre--summary" aria-hidden="false">
          {summary}
        </pre>
      )}
    </>
  )

  return (
    <div className="thinking-panel" data-thinking-request-id={request_id}>
      <div
        className="thinking-panel-region-inner"
        role="region"
        aria-label="Thinking"
        tabIndex={phase === 'complete' ? 0 : undefined}
        onClick={() => {
          if (phase === 'complete') setExpanded((e) => !e)
        }}
        onKeyDown={(e) => {
          if (phase !== 'complete') return
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setExpanded((ex) => !ex)
          }
        }}
      >
        {shell}
      </div>
    </div>
  )
}
