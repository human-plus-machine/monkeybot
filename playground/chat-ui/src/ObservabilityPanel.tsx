function resolveUrl(envKey: string, fallback: string): string {
  const raw = (import.meta.env[envKey] as string | undefined)?.trim()
  return raw && raw.length > 0 ? raw.replace(/\/$/, '') : fallback
}

const PHOENIX_URL = resolveUrl('VITE_PHOENIX_URL', 'http://localhost:6006')
const LANGFUSE_URL = resolveUrl('VITE_LANGFUSE_URL', 'http://localhost:3000')

type Props = {
  lastTraceId: string | null
  observabilityEnabled: boolean
}

export default function ObservabilityPanel({ lastTraceId, observabilityEnabled }: Props) {
  const phoenixTraceUrl =
    lastTraceId != null
      ? `${PHOENIX_URL}/projects/default/traces/${lastTraceId}`
      : PHOENIX_URL

  return (
    <div className="observability-pane">
      <h2 className="panel-heading">Observability</h2>
      <p className="observability-intro">
        Traces export via OpenTelemetry when the gateway runs with{' '}
        <code className="obs-inline-code">MONKEYBOT_OTEL_ENABLED=true</code>. Playground{' '}
        <code className="obs-inline-code">run.sh</code> starts Phoenix, Langfuse, and an OTel
        collector on Docker.
      </p>

      <section className="obs-section" aria-labelledby="obs-status-heading">
        <h3 id="obs-status-heading" className="obs-section-title">
          Gateway tracing
        </h3>
        <p className="obs-status-line">
          <span
            className={`obs-pill ${observabilityEnabled ? 'obs-pill--on' : 'obs-pill--off'}`}
          >
            {observabilityEnabled ? 'OTel enabled (last turn)' : 'No trace_id on last turn'}
          </span>
        </p>
        {lastTraceId ? (
          <p className="obs-trace-id" title={lastTraceId}>
            trace_id: <code>{lastTraceId}</code>
          </p>
        ) : (
          <p className="obs-muted">Send a message while tracing is on to capture a trace_id.</p>
        )}
      </section>

      <section className="obs-section" aria-labelledby="obs-backends-heading">
        <h3 id="obs-backends-heading" className="obs-section-title">
          Trace backends
        </h3>
        <div className="obs-link-grid">
          <a
            className="obs-backend-card"
            href={phoenixTraceUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span className="obs-backend-name">Phoenix</span>
            <span className="obs-backend-url">{PHOENIX_URL}</span>
            <span className="obs-backend-hint">
              {lastTraceId ? 'Open latest trace' : 'Open Phoenix UI'}
            </span>
          </a>
          <a
            className="obs-backend-card"
            href={LANGFUSE_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span className="obs-backend-name">Langfuse</span>
            <span className="obs-backend-url">{LANGFUSE_URL}</span>
            <span className="obs-backend-hint">Traces → project (after dual export)</span>
          </a>
        </div>
      </section>

      <section className="obs-section" aria-labelledby="obs-setup-heading">
        <h3 id="obs-setup-heading" className="obs-section-title">
          Local stack
        </h3>
        <ul className="obs-checklist">
          <li>
            <strong>OTLP endpoint:</strong>{' '}
            <code className="obs-inline-code">http://127.0.0.1:4318</code>
          </li>
          <li>
            <strong>Collector → Phoenix</strong> (always when stack is up)
          </li>
          <li>
            <strong>Collector → Langfuse</strong> after setting{' '}
            <code className="obs-inline-code">LANGFUSE_OTEL_BASIC_AUTH</code> in{' '}
            <code className="obs-inline-code">playground/agent/.env</code>
          </li>
          <li>
            Default Langfuse login (init): <code>admin@example.com</code> / <code>password</code>
          </li>
        </ul>
        <pre className="obs-env-pre">{`MONKEYBOT_OTEL_ENABLED=true
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none
OTEL_SERVICE_NAME=monkeybot-gateway
# Optional dual export (pk:sk from Langfuse → API keys):
# LANGFUSE_OTEL_BASIC_AUTH=Basic $(printf '%s' 'pk-...:sk-...' | base64)`}</pre>
      </section>
    </div>
  )
}
