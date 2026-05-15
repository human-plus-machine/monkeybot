import { useState } from 'react'
import type { PendingWidget } from '../App.tsx'
import { postElicitation } from '../gatewayClient'

export type ElicitationFormProps = {
  sessionId: string
  widget: Extract<PendingWidget, { kind: 'elicitation' }>
  onResolved: () => void
  onError: (message: string) => void
}

type FlatField = { name: string; control: 'string' | 'number' | 'integer' | 'boolean' | 'enum'; enumVals?: string[] }

function describeFlatFields(requested_schema: Record<string, unknown>): { ok: true; fields: FlatField[] } | { ok: false } {
  if (requested_schema.type !== 'object') return { ok: false }
  const props = requested_schema.properties
  if (!props || typeof props !== 'object') return { ok: false }
  const requiredRaw = requested_schema.required
  const requiredList = Array.isArray(requiredRaw) ? requiredRaw.map(String) : []
  const fields: FlatField[] = []
  for (const name of Object.keys(props as Record<string, unknown>)) {
    const p = (props as Record<string, unknown>)[name]
    if (!p || typeof p !== 'object') return { ok: false }
    const t = (p as { type?: unknown }).type
    if (t === 'string') {
      const en = (p as { enum?: unknown }).enum
      if (en !== undefined) {
        if (!Array.isArray(en) || en.some((x) => typeof x !== 'string')) return { ok: false }
        fields.push({ name, control: 'enum', enumVals: en as string[] })
      } else {
        fields.push({ name, control: 'string' })
      }
    } else if (t === 'number') {
      fields.push({ name, control: 'number' })
    } else if (t === 'integer') {
      fields.push({ name, control: 'integer' })
    } else if (t === 'boolean') {
      fields.push({ name, control: 'boolean' })
    } else {
      return { ok: false }
    }
  }
  for (const r of requiredList) {
    if (!fields.some((f) => f.name === r)) return { ok: false }
  }
  return { ok: true, fields }
}

function coerceField(f: FlatField, raw: string): unknown {
  if (f.control === 'boolean') return raw === 'true'
  if (f.control === 'integer') return Number.parseInt(raw, 10)
  if (f.control === 'number') return Number.parseFloat(raw)
  return raw
}

/**
 * Flat JSON-Schema object fields → `postElicitation` with `user_data`. Unsupported schemas submit `user_data: null`.
 */
export default function ElicitationForm({ widget, sessionId, onResolved, onError }: ElicitationFormProps) {
  const parsed = describeFlatFields(widget.requested_schema)
  const [values, setValues] = useState<Record<string, string>>(() => {
    if (!parsed.ok) return {}
    const init: Record<string, string> = {}
    for (const f of parsed.fields) {
      init[f.name] = ''
    }
    return init
  })
  const [inlineError, setInlineError] = useState('')
  const [busy, setBusy] = useState(false)

  const postUserData = async (user_data: unknown) => {
    setInlineError('')
    setBusy(true)
    try {
      await postElicitation(sessionId, widget.id, { user_data })
      onResolved()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setInlineError(msg)
      onError(msg)
    } finally {
      setBusy(false)
    }
  }

  const handleSubmitFlat = (e: React.FormEvent) => {
    e.preventDefault()
    if (!parsed.ok) return
    const user_data: Record<string, unknown> = {}
    for (const f of parsed.fields) {
      user_data[f.name] = coerceField(f, values[f.name] ?? '')
    }
    void postUserData(user_data)
  }

  const handleRejectFlat = () => void postUserData(null)

  const handleUnsupportedSubmit = () => void postUserData(null)

  const handleUnsupportedReject = () => void postUserData(null)

  if (!parsed.ok) {
    return (
      <div className="chat-modal-overlay" role="presentation" data-testid="elicitation-form">
        <div className="chat-modal-shell" role="dialog" aria-modal="true" aria-label="Elicitation">
          <p>{widget.message}</p>
          <p className="error">Schema not supported in v1</p>
          <div className="chat-modal-actions row">
            <button type="button" className="btn primary" disabled={busy} onClick={handleUnsupportedSubmit}>
              Submit
            </button>
            <button type="button" className="btn" disabled={busy} onClick={handleUnsupportedReject}>
              Reject
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

  const fields = parsed.fields

  return (
    <div className="chat-modal-overlay" role="presentation" data-testid="elicitation-form">
      <div className="chat-modal-shell" role="dialog" aria-modal="true" aria-label="Elicitation">
        <p>{widget.message}</p>
        <form onSubmit={handleSubmitFlat}>
          {fields.map((f) =>
            f.control === 'enum' && f.enumVals ? (
              <label key={f.name} className="elicitation-field">
                {f.name}
                <select
                  value={values[f.name] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                  aria-label={f.name}
                  required
                  disabled={busy}
                >
                  <option value="">—</option>
                  {f.enumVals.map((ev) => (
                    <option key={ev} value={ev}>
                      {ev}
                    </option>
                  ))}
                </select>
              </label>
            ) : f.control === 'boolean' ? (
              <label key={f.name} className="elicitation-field">
                <input
                  type="checkbox"
                  checked={values[f.name] === 'true'}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.checked ? 'true' : '' }))}
                  aria-label={f.name}
                  disabled={busy}
                />{' '}
                {f.name}
              </label>
            ) : (
              <label key={f.name} className="elicitation-field">
                {f.name}
                <input
                  type={f.control === 'number' || f.control === 'integer' ? 'number' : 'text'}
                  value={values[f.name] ?? ''}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                  aria-label={f.name}
                  required
                  disabled={busy}
                />
              </label>
            ),
          )}
          <div className="chat-modal-actions row">
            <button type="submit" className="btn primary" disabled={busy}>
              Submit
            </button>
            <button type="button" className="btn" disabled={busy} onClick={handleRejectFlat}>
              Reject
            </button>
          </div>
        </form>
        {inlineError ? (
          <div className="error chat-modal-inline-error" role="alert">
            {inlineError}
          </div>
        ) : null}
      </div>
    </div>
  )
}
