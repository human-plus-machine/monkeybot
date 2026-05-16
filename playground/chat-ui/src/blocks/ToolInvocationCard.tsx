import { useEffect, useState } from 'react'

const MAX_RESULT_CHARS = 24_000

const SUMMARY_MAX = 140

function oneLine(s: string, max = SUMMARY_MAX): string {
  const t = s.replace(/\s+/g, ' ').trim()
  if (!t) return ''
  return t.length <= max ? t : `${t.slice(0, Math.max(0, max - 1))}…`
}

/** Short hint for the collapsed summary row (best-effort from common arg shapes). */
export function toolExecutionSummary(args?: Record<string, unknown>): string {
  if (!args || typeof args !== 'object') return ''
  const cmd = args.command
  if (typeof cmd === 'string' && cmd.trim()) return oneLine(cmd)
  const path = args.path
  if (typeof path === 'string' && path.trim()) return oneLine(path)
  const query = args.query
  if (typeof query === 'string' && query.trim()) return oneLine(query)
  const url = args.url
  if (typeof url === 'string' && url.trim()) return oneLine(url)
  const keys = Object.keys(args)
  if (keys.length === 0) return ''
  if (keys.length === 1) {
    const k = keys[0]
    const v = args[k]
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      return oneLine(`${k}: ${String(v)}`)
    }
  }
  return `${keys.length} argument${keys.length === 1 ? '' : 's'}`
}

function safePrettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function formatArgs(args: Record<string, unknown> | undefined): string {
  if (!args || typeof args !== 'object' || Object.keys(args).length === 0) {
    return ''
  }
  return safePrettyJson(args)
}

function formatResultBody(raw: string | undefined): string {
  if (!raw?.trim()) {
    return '(empty result)'
  }
  const t = raw.trim()
  try {
    const j = JSON.parse(t) as unknown
    if (j !== null && (typeof j === 'object' || Array.isArray(j))) {
      return safePrettyJson(j)
    }
  } catch {
    /* plain text */
  }
  return t
}

function IconCheck() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M20 6L9 17l-5-5"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function IconError() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M18 6 6 18M6 6l12 12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  )
}

export type ToolInvocationCardProps = {
  tool: string
  label?: string
  args?: Record<string, unknown>
  phase: 'running' | 'complete'
  error?: string
  /** Raw tool result string from the gateway (often JSON). */
  resultRaw?: string
  /** True when result was clipped before storage. */
  resultTruncated?: boolean
}

function StatusInline({ phase, error }: { phase: 'running' | 'complete'; error?: string }) {
  const done = phase === 'complete'
  return (
    <span className="tool-inv-status tool-inv-status--inline">
      {!done ? (
        <>
          <span className="tool-inv-spinner" aria-hidden />
          <span>Running</span>
        </>
      ) : error ? (
        <>
          <span className="tool-inv-status-icon tool-inv-status-icon--error" aria-hidden>
            <IconError />
          </span>
          <span>Failed</span>
        </>
      ) : (
        <>
          <span className="tool-inv-status-icon tool-inv-status-icon--ok" aria-hidden>
            <IconCheck />
          </span>
          <span>Done</span>
        </>
      )}
    </span>
  )
}

export default function ToolInvocationCard({
  tool,
  label,
  args,
  phase,
  error,
  resultRaw,
  resultTruncated,
}: ToolInvocationCardProps) {
  const argsBlock = formatArgs(args)
  const showLabel = label && label !== tool
  const done = phase === 'complete'
  const hint = toolExecutionSummary(args)
  const [expanded, setExpanded] = useState(() => phase === 'running')

  useEffect(() => {
    if (phase === 'complete') setExpanded(false)
  }, [phase])

  const summaryHint = hint || (argsBlock ? 'see arguments' : 'no arguments')

  return (
    <div className="tool-invocation-card" data-testid="tool-invocation-card" aria-busy={phase === 'running'}>
      <details
        className="tool-inv-details"
        open={expanded}
        onToggle={(e) => {
          const el = e.currentTarget
          setExpanded(el.open)
        }}
      >
        <summary className="tool-inv-summary">
          <span className="tool-inv-summary-main">
            <span className="tool-inv-pill" title={tool}>
              {tool}
            </span>
            {showLabel ? (
              <span className="tool-inv-subtitle tool-inv-subtitle--summary" title={label}>
                {label}
              </span>
            ) : null}
            <StatusInline phase={phase} error={error} />
          </span>
          <span className="tool-inv-summary-hint" title={summaryHint}>
            {summaryHint}
          </span>
        </summary>

        <div className="tool-inv-details-body">
          <div className="tool-inv-section">
            <div className="tool-inv-section-label">Arguments</div>
            {argsBlock ? (
              <pre className="tool-inv-pre">{argsBlock}</pre>
            ) : (
              <p className="tool-inv-empty-args">(no arguments)</p>
            )}
          </div>

          {done ? (
            <div className="tool-inv-section">
              {error ? (
                <>
                  <div className="tool-inv-section-label">Error</div>
                  <pre className="tool-inv-pre tool-inv-pre--error">{error}</pre>
                </>
              ) : (
                <>
                  <div className="tool-inv-section-label">Result</div>
                  <pre className="tool-inv-pre">{formatResultBody(resultRaw)}</pre>
                  {resultTruncated ? (
                    <p className="tool-inv-trunc-note">
                      Output truncated for display (over {MAX_RESULT_CHARS.toLocaleString()} characters).
                    </p>
                  ) : null}
                </>
              )}
            </div>
          ) : null}
        </div>
      </details>
    </div>
  )
}

export { MAX_RESULT_CHARS }
