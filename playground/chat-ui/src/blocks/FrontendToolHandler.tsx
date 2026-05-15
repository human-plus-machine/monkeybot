import { useEffect } from 'react'
import type { PendingWidget } from '../App.tsx'
import { postFrontendToolResult } from '../gatewayClient'
import { frontendToolRegistry } from '../frontendToolRegistry'

const runningFrontendTools = new Set<string>()

export type FrontendToolHandlerProps = {
  sessionId: string
  widget: Extract<PendingWidget, { kind: 'frontendTool' }>
  onResolved: () => void
  onError: (message: string) => void
}

/**
 * Runs a registered frontend tool and POSTs the result to the gateway.
 */
export default function FrontendToolHandler({ widget, sessionId, onResolved, onError }: FrontendToolHandlerProps) {
  useEffect(() => {
    const key = `${sessionId}:${widget.tool_call_id}`
    if (runningFrontendTools.has(key)) return
    runningFrontendTools.add(key)

    ;(async () => {
      const fn = frontendToolRegistry[widget.name]
      if (!fn) {
        try {
          await postFrontendToolResult(sessionId, widget.tool_call_id, {
            is_error: true,
            result: [{ type: 'text', text: `no frontend handler for tool '${widget.name}'` }],
          })
          onResolved()
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e)
          onError(msg)
        } finally {
          runningFrontendTools.delete(key)
        }
        return
      }

      try {
        const out = await fn(widget.args)
        await postFrontendToolResult(sessionId, widget.tool_call_id, {
          result: out.result,
          is_error: out.is_error,
        })
        onResolved()
      } catch (e) {
        const text = e instanceof Error ? e.message : String(e)
        try {
          await postFrontendToolResult(sessionId, widget.tool_call_id, {
            is_error: true,
            result: [{ type: 'text', text }],
          })
          onResolved()
        } catch (e2) {
          const msg = e2 instanceof Error ? e2.message : String(e2)
          onError(msg)
        }
      } finally {
        runningFrontendTools.delete(key)
      }
    })()
  }, [onError, onResolved, sessionId, widget.args, widget.name, widget.tool_call_id])

  return (
    <div
      data-testid="frontend-tool-handler-stub"
      className="chat-modal-shell chat-modal-shell--inline"
      role="status"
      aria-label="Frontend tool"
    >
      <p className="muted">Running {widget.name}…</p>
    </div>
  )
}
