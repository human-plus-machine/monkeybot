import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './evals-panel.css'

type Scenario = {
  id: string
  description: string
  tags: string[]
  messages: string[]
  assertions: Record<string, unknown>
  source: 'builtin' | 'operator'
}

type TurnResult = {
  input: string
  output: string
  trace_id?: string | null
  scores?: Record<string, number>
  reasons?: Record<string, string>
}

type EvalRun = {
  run_id: string
  scenario_id: string
  status: 'running' | 'completed' | 'failed'
  turns: TurnResult[]
  scores: Record<string, number>
  pass_rate: number | null
  created_at: string
  error?: string | null
  score_details: Array<{
    metric: string
    score: number | null
    reason?: string | null
    success?: boolean | null
    error?: string | null
    test_index?: number | null
  }>
}

type WsEnvelope = { type: string; payload: Record<string, unknown> }

function evalsBase(): string {
  const raw = (import.meta.env.VITE_EVALS_URL as string | undefined)?.trim()
  if (raw && raw.length > 0) return raw.replace(/\/$/, '')
  return '/__mb_evals'
}

function evalsApi(path: string): string {
  const p = path.startsWith('/') ? path : `/${path}`
  const b = evalsBase()
  if (b.startsWith('http')) return `${b}${p}`
  return `${b}${p}`
}

function evalsWsUrl(runId: string): string {
  const b = evalsBase()
  const path = `/ws/runs/${encodeURIComponent(runId)}`
  if (b.startsWith('http')) {
    const u = new URL(b)
    const proto = u.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${u.host}${path}`
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${b}${path}`
}

function langfuseTraceUrl(traceId: string): string {
  const base =
    (import.meta.env.VITE_LANGFUSE_URL as string | undefined)?.replace(/\/$/, '') || 'http://localhost:3000'
  return `${base}/traces/${encodeURIComponent(traceId)}`
}

export default function EvalsPanel() {
  const [activeTab, setActiveTab] = useState<'scenarios' | 'runs' | 'live'>('scenarios')
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [runs, setRuns] = useState<EvalRun[]>([])
  const [yamlDraft, setYamlDraft] = useState(`id: my_custom_scenario
description: "Quick smoke scenario"
tags: [smoke]
messages:
  - "Say hello in one short sentence."
assertions:
  metrics: [response_relevancy]
`)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null)
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  const [liveRun, setLiveRun] = useState<EvalRun | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const refreshScenarios = useCallback(async () => {
    const r = await fetch(evalsApi('/api/scenarios'))
    if (!r.ok) throw new Error(`scenarios ${r.status}`)
    setScenarios((await r.json()) as Scenario[])
  }, [])

  const refreshRuns = useCallback(async () => {
    const r = await fetch(evalsApi('/api/runs'))
    if (!r.ok) throw new Error(`runs ${r.status}`)
    setRuns((await r.json()) as EvalRun[])
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        await refreshScenarios()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    })()
  }, [refreshScenarios])

  useEffect(() => {
    if (activeTab !== 'runs') return
    void (async () => {
      try {
        await refreshRuns()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    })()
  }, [activeTab, refreshRuns])

  const startRun = async (scenarioId: string) => {
    setBusy(true)
    setError(null)
    try {
      const r = await fetch(evalsApi('/api/runs'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scenario_id: scenarioId }),
      })
      if (!r.ok) throw new Error(await r.text())
      const body = (await r.json()) as { run_id: string }
      setLiveRunId(body.run_id)
      setLiveRun({
        run_id: body.run_id,
        scenario_id: scenarioId,
        status: 'running',
        turns: [],
        scores: {},
        pass_rate: null,
        created_at: new Date().toISOString(),
        score_details: [],
      })
      setActiveTab('live')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const saveScenario = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await fetch(evalsApi('/api/scenarios'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml: yamlDraft }),
      })
      if (!r.ok) throw new Error(await r.text())
      await refreshScenarios()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (!liveRunId) return
    const ws = new WebSocket(evalsWsUrl(liveRunId))
    wsRef.current = ws
    ws.onmessage = (evt) => {
      let msg: WsEnvelope
      try {
        msg = JSON.parse(evt.data as string) as WsEnvelope
      } catch {
        return
      }
      if (msg.type === 'snapshot') {
        const p = msg.payload as EvalRun
        setLiveRun(p)
        if (p.status === 'failed' && p.error) {
          setError(p.error)
        }
        if (p.status === 'completed' || p.status === 'failed') {
          void refreshRuns()
        }
      }
      if (msg.type === 'turn') {
        const p = msg.payload as {
          turn_index: number
          input: string
          output: string
          trace_id?: string | null
        }
        setLiveRun((prev) => {
          if (!prev || prev.run_id !== liveRunId) return prev
          const turns = [...prev.turns]
          while (turns.length <= p.turn_index) {
            turns.push({ input: '', output: '', trace_id: null })
          }
          turns[p.turn_index] = {
            input: p.input,
            output: p.output,
            trace_id: p.trace_id ?? null,
          }
          return { ...prev, turns, status: 'running' }
        })
      }
      if (msg.type === 'scores') {
        const p = msg.payload as {
          scores: Record<string, number>
          pass_rate: number | null
          details: EvalRun['score_details']
        }
        setLiveRun((prev) => {
          if (!prev || prev.run_id !== liveRunId) return prev
          return {
            ...prev,
            scores: p.scores || {},
            pass_rate: p.pass_rate ?? null,
            score_details: p.details || [],
          }
        })
      }
      if (msg.type === 'status') {
        const st = String((msg.payload as { status?: string }).status || '')
        if (st === 'completed' || st === 'failed' || st === 'running') {
          setLiveRun((prev) => {
            if (!prev || prev.run_id !== liveRunId) return prev
            return { ...prev, status: st as EvalRun['status'] }
          })
        }
        if (st === 'completed' || st === 'failed') {
          void (async () => {
            try {
              const r = await fetch(evalsApi(`/api/runs/${encodeURIComponent(liveRunId)}`))
              if (r.ok) {
                setLiveRun((await r.json()) as EvalRun)
              }
            } catch {
              /* ignore */
            }
            void refreshRuns()
          })()
        }
      }
      if (msg.type === 'error') {
        const m = String((msg.payload as { message?: string }).message || 'error')
        setError(m)
      }
    }
    return () => {
      ws.close()
      wsRef.current = null
    }
  }, [liveRunId, refreshRuns])

  const expanded = useMemo(
    () => runs.find((x) => x.run_id === expandedRunId) || null,
    [runs, expandedRunId],
  )

  return (
    <div className="evals-pane">
      <h2 className="panel-heading">Evals</h2>
      <p className="evals-intro">
        Quality runs against the live gateway via the evals service (
        <code className="obs-inline-code">{evalsBase()}</code>
        ). Configure the target with <code className="obs-inline-code">VITE_EVALS_URL</code> or the
        dev proxy <code className="obs-inline-code">/__mb_evals</code>.
      </p>
      {error ? <p className="ev-muted">Error: {error}</p> : null}

      <div className="evals-subtabs" role="tablist">
        <button
          type="button"
          role="tab"
          className="evals-subtab"
          aria-selected={activeTab === 'scenarios'}
          onClick={() => setActiveTab('scenarios')}
        >
          Scenarios
        </button>
        <button
          type="button"
          role="tab"
          className="evals-subtab"
          aria-selected={activeTab === 'runs'}
          onClick={() => setActiveTab('runs')}
        >
          Runs
        </button>
        <button
          type="button"
          role="tab"
          className="evals-subtab"
          aria-selected={activeTab === 'live'}
          onClick={() => setActiveTab('live')}
        >
          Live
        </button>
      </div>

      <div className="evals-scroll" role="tabpanel">
        {activeTab === 'scenarios' ? (
          <div>
            <h3 className="ev-section-title">Built-in & operator scenarios</h3>
            {scenarios.map((s) => (
              <div key={s.id} className="ev-scenario-card">
                <div className="ev-scenario-head">
                  <div>
                    <p className="ev-scenario-id">{s.id}</p>
                    <p className="ev-scenario-desc">{s.description || '—'}</p>
                  </div>
                  <span className={s.source === 'builtin' ? 'ev-badge-builtin' : 'ev-badge-operator'}>
                    {s.source}
                  </span>
                </div>
                <div className="ev-tags">
                  {s.tags.map((t) => (
                    <span key={t} className="ev-tag">
                      {t}
                    </span>
                  ))}
                </div>
                <div className="ev-btn-row">
                  <button
                    type="button"
                    className="ev-btn ev-btn-primary"
                    disabled={busy}
                    onClick={() => void startRun(s.id)}
                  >
                    Run
                  </button>
                </div>
              </div>
            ))}

            <h3 className="ev-section-title" style={{ marginTop: 18 }}>
              New scenario (YAML)
            </h3>
            <textarea className="ev-textarea" value={yamlDraft} onChange={(e) => setYamlDraft(e.target.value)} />
            <div className="ev-btn-row">
              <button type="button" className="ev-btn ev-btn-primary" disabled={busy} onClick={() => void saveScenario()}>
                Save scenario
              </button>
            </div>
          </div>
        ) : null}

        {activeTab === 'runs' ? (
          <div>
            <h3 className="ev-section-title">Recent runs</h3>
            <table className="ev-table">
              <thead>
                <tr>
                  <th>Scenario</th>
                  <th>Status</th>
                  <th>Pass rate</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr
                    key={r.run_id}
                    onClick={() => setExpandedRunId(expandedRunId === r.run_id ? null : r.run_id)}
                  >
                    <td>{r.scenario_id}</td>
                    <td>
                      <span
                        className={`ev-pill ${
                          r.status === 'running'
                            ? 'ev-pill-running'
                            : r.status === 'completed'
                              ? 'ev-pill-done'
                              : 'ev-pill-fail'
                        }`}
                      >
                        {r.status}
                      </span>
                    </td>
                    <td>{r.pass_rate != null ? r.pass_rate.toFixed(2) : '—'}</td>
                    <td className="ev-muted">{r.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {expanded ? (
              <div className="ev-detail">
                <div>
                  <strong>{expanded.run_id}</strong> · scores: {JSON.stringify(expanded.scores)}
                </div>
                {expanded.turns.map((t, i) => (
                  <div key={i} style={{ marginTop: 10 }}>
                    <div>
                      <span className="ev-muted">Turn {i + 1}</span>
                      {t.trace_id ? (
                        <>
                          {' '}
                          ·{' '}
                          <a className="ev-link" href={langfuseTraceUrl(t.trace_id)} target="_blank" rel="noreferrer">
                            Langfuse trace
                          </a>
                        </>
                      ) : null}
                    </div>
                    <div>Q: {t.input}</div>
                    <div>A: {t.output}</div>
                  </div>
                ))}
                {expanded.score_details?.length ? (
                  <div style={{ marginTop: 10 }}>
                    <div className="ev-muted">Metric details</div>
                    {expanded.score_details.map((d, i) => (
                      <div key={i} style={{ marginTop: 6 }}>
                        <strong>{d.metric}</strong>: {d.score != null ? d.score.toFixed(3) : '—'}
                        {d.reason ? <div className="ev-muted">{d.reason}</div> : null}
                        {d.error ? <div className="ev-muted">err: {d.error}</div> : null}
                      </div>
                    ))}
                  </div>
                ) : null}
                {expanded.error ? <div className="ev-muted">Error: {expanded.error}</div> : null}
              </div>
            ) : null}
          </div>
        ) : null}

        {activeTab === 'live' ? (
          <div>
            <h3 className="ev-section-title">Live run</h3>
            {!liveRunId ? <p className="ev-muted">Start a run from the Scenarios tab.</p> : null}
            {liveRun ? (
              <div>
                <p className="ev-muted">
                  Run <code className="obs-inline-code">{liveRun.run_id}</code> · {liveRun.scenario_id} ·{' '}
                  <span
                    className={`ev-pill ${
                      liveRun.status === 'running'
                        ? 'ev-pill-running'
                        : liveRun.status === 'completed'
                          ? 'ev-pill-done'
                          : 'ev-pill-fail'
                    }`}
                  >
                    {liveRun.status}
                  </span>
                </p>
                {liveRun.turns.map((t, i) => (
                  <div key={i} className="ev-live-card">
                    <div className="ev-live-label">User</div>
                    <div className="ev-live-body">{t.input}</div>
                    <div className="ev-live-label" style={{ marginTop: 8 }}>
                      Assistant
                    </div>
                    <div className="ev-live-body">{t.output || '…'}</div>
                    {t.trace_id ? (
                      <a className="ev-link" href={langfuseTraceUrl(t.trace_id)} target="_blank" rel="noreferrer">
                        Open trace in Langfuse
                      </a>
                    ) : null}
                  </div>
                ))}
                {Object.keys(liveRun.scores).length ? (
                  <div style={{ marginTop: 8 }}>
                    {Object.entries(liveRun.scores).map(([k, v]) => (
                      <span key={k} className="ev-score-chip">
                        {k}: {v.toFixed(2)}
                      </span>
                    ))}
                    {liveRun.pass_rate != null ? (
                      <span className="ev-score-chip">pass_rate: {liveRun.pass_rate.toFixed(2)}</span>
                    ) : null}
                  </div>
                ) : null}
                {liveRun.score_details?.length ? (
                  <div style={{ marginTop: 12 }}>
                    <div className="ev-live-label">deepeval reasoning</div>
                    {liveRun.score_details.map((d, i) => (
                      <div key={i} className="ev-live-card">
                        <strong>{d.metric}</strong>: {d.score != null ? d.score.toFixed(3) : '—'}
                        {d.reason ? <div className="ev-muted">{d.reason}</div> : null}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}
