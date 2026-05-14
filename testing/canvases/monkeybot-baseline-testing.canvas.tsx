import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Code,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
  useHostTheme,
} from "cursor/canvas";

// ── Baseline data ──────────────────────────────────────────────────────────────

const OFFLINE_DATE = "May 13, 2026 — macOS, no LLM — 13/13 PASS";

type BenchRow = [string, string, string, string];

const coldStartRows: BenchRow[] = [
  ["import monkeybot", "13 ms", "2000 ms", "PASS"],
  ["monkeybot --help (uv)", "72 ms", "5000 ms", "PASS"],
  ["import AgentLoop", "46 ms", "1500 ms", "PASS"],
  ["import GeminiProvider", "33 ms", "2000 ms", "PASS"],
];

const harnessRows: BenchRow[] = [
  ["First turn (DB init included)", "3.2 ms", "500 ms", "PASS"],
  ["TTFT — fake provider", "1.9 ms", "100 ms", "PASS"],
  ["Warm turn avg (×5)", "2.6 ms", "200 ms", "PASS"],
  ["Streaming (100 deltas)", "2.6 ms / ~38 812 tok/s", "—", "—"],
];

const memoryRows: BenchRow[] = [
  ["Write 10 memory files", "1.0 ms", "200 ms", "PASS*"],
  ["search_memory (10 files)", "0.3 ms", "50 ms", "PASS*"],
  ["Direct file read", "~0 ms", "20 ms", "PASS*"],
];

const sqliteRows: BenchRow[] = [
  ["Save 20 messages", "18 ms", "500 ms", "PASS"],
  ["Load 20 messages", "0.8 ms", "50 ms", "PASS"],
];

// Live LLM — May 14 2026, macOS, Vertex Claude
type LiveRow = [string, string, string, string, string];
const liveLlmRows: LiveRow[] = [
  ["vertex-claude", "claude-sonnet-4-6@default", "1873 ms", "1375 ms", "2134 ms / 1552 ms"],
  ["gemini", "gemini-2.0-flash", "TBD", "TBD", "TBD / TBD"],
  ["claude", "claude-sonnet (direct)", "TBD (no key)", "TBD", "TBD / TBD"],
];

// Docker — not yet recorded
const dockerRows: BenchRow[] = [
  ["/health response", "TBD", "500 ms", "—"],
  ["/webhook e2e", "TBD", "10 000 ms", "—"],
  ["3× concurrent /webhook", "TBD", "—", "—"],
];

const featureRows: [string, string, string][] = [
  ["Skills discovery & YAML loader", "tests/skills/test_loader.py", "Covered"],
  ["Skills execution (file-ops)", "tests/integration/test_skills_integration.py", "Covered"],
  ["Memory search (INDEX.md)", "tests/core/test_memory.py", "Covered"],
  ["Council run + index update", "tests/core/test_council.py", "Covered"],
  ["Gateway SSE sessions", "tests/integration/test_mb_e2e_simple_reply.py", "Covered"],
  ["SQLite history per thread", "tests/core/test_history.py", "Covered"],
  ["save_memory tool (write path)", "bench.py §3 — API mismatch", "Broken"],
  ["Cross-session memory persistence", "— not automated", "Gap"],
  ["Container cold start timing", "— not measured", "Gap"],
  ["Live LLM TTFT (all providers)", "bench.py §5 --live", "No numbers yet"],
  ["Docker HTTP baseline", "bench.py §6 --docker", "No numbers yet"],
  ["Memory accuracy verification", "— BACKLOG item", "Gap"],
];

const criteriaRows: [string, string, string][] = [
  ["Cold start (import)", "< 200 ms", "13 ms — well under"],
  ["Cold start (first token)", "< 500 ms", "TBD (needs --live)"],
  ["Hard runtime dependencies", "≤ 6", "Not yet counted"],
  ["Agent loop LOC", "≤ 500 in one file", "Not yet measured"],
  ["Files to add a new skill", "1", "Meets target"],
  ["Files to add a new provider", "1", "Meets target"],
  ["Cloud SDKs in core image", "0", "Meets target"],
];

// ── helpers ────────────────────────────────────────────────────────────────────

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "PASS" || status === "PASS*" || status === "Covered" || status === "Meets target"
      ? "success"
      : status === "—" || status === "Not yet counted" || status === "Not yet measured"
      ? "neutral"
      : status === "TBD" || status === "No numbers yet" || status === "TBD (needs --live)"
      ? "warning"
      : "danger";
  return <Pill tone={tone} size="small">{status}</Pill>;
}

function BenchTable({ rows, note }: { rows: BenchRow[]; note?: string }) {
  return (
    <Stack gap={6}>
      <Table
        headers={["Test", "Result", "Limit", "Status"]}
        rows={rows.map(([test, result, limit, status]) => [
          test,
          result,
          limit,
          <StatusPill key={test} status={status} />,
        ])}
      />
      {note && <Text tone="secondary" size="small">{note}</Text>}
    </Stack>
  );
}

// ── canvas ────────────────────────────────────────────────────────────────────

export default function MonkeyBotBaseline() {
  return (
    <Stack gap={28} style={{ padding: "24px 28px", maxWidth: 960 }}>

      {/* Header */}
      <Stack gap={4}>
        <H1>MonkeyBot — Baseline Testing Guide</H1>
        <Text tone="secondary" size="small">
          Reference snapshot for performance and feature coverage. Update after every
          significant run. Source of truth: <Code>testing/BENCH_NOTES.md</Code>
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="12 / 12" label="Offline tests passing" tone="success" />
        <Stat value="1 / 3" label="Live provider baselines recorded" tone="warning" />
        <Stat value="4" label="Known gaps" tone="warning" />
        <Stat value="~400 ms" label="Full offline suite" />
      </Grid>

      <Divider />

      {/* How to run */}
      <Stack gap={12}>
        <H2>How to Run</H2>
        <Grid columns={2} gap={12}>
          <Card>
            <CardHeader>Offline — no key (~400 ms)</CardHeader>
            <CardBody>
              <Code>uv run python testing/bench.py</Code>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Live — Vertex Claude</CardHeader>
            <CardBody>
              <Stack gap={10}>
                <Text size="small" tone="secondary">
                  Set <Code>GOOGLE_APPLICATION_CREDENTIALS</Code> once — project ID is read automatically.
                </Text>
                <Stack gap={3}>
                  <Text size="small" weight="bold">John</Text>
                  <Code>/Users/johnpiscani/ez-ai/auriga/gcp_service_auth_qa.json</Code>
                </Stack>
                <Stack gap={3}>
                  <Text size="small" weight="bold">Karthik</Text>
                  <Code>/Users/kz127/code/GCP_KEYS/gcp_service_auth_qa.json</Code>
                </Stack>
                <Stack gap={3}>
                  <Text size="small" weight="bold">Everyone else</Text>
                  <Code>/path/to/gcp_service_auth_qa.json</Code>
                </Stack>
                <Stack gap={3}>
                  <Text size="small" tone="secondary">Then run:</Text>
                  <Code>export GOOGLE_APPLICATION_CREDENTIALS=&lt;path above&gt;</Code>
                  <Code>uv run python testing/bench.py --live</Code>
                </Stack>
              </Stack>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Live — Gemini / Anthropic direct</CardHeader>
            <CardBody>
              <Stack gap={4}>
                <Code>GEMINI_API_KEY=... MODEL_PROVIDER=gemini uv run python testing/bench.py --live</Code>
                <Code>ANTHROPIC_API_KEY=... MODEL_PROVIDER=claude uv run python testing/bench.py --live</Code>
              </Stack>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Docker + Pytest</CardHeader>
            <CardBody>
              <Stack gap={4}>
                <Code>docker compose -f docker/docker-compose.yml up -d</Code>
                <Code>uv run python testing/bench.py --live --docker</Code>
                <Code>uv run pytest tests/ -v</Code>
              </Stack>
            </CardBody>
          </Card>
        </Grid>
      </Stack>

      <Divider />

      {/* Offline baseline */}
      <Stack gap={16}>
        <Stack gap={2}>
          <H2>Offline Performance Baseline</H2>
          <Text tone="secondary" size="small">{OFFLINE_DATE}</Text>
        </Stack>

        <H3>1 — Cold Start</H3>
        <BenchTable rows={coldStartRows} />

        <H3>2 — Harness TTFT (Fake Provider)</H3>
        <BenchTable rows={harnessRows} />

        <H3>3 — Memory Ops</H3>
        <BenchTable
          rows={memoryRows}
          note="* Numbers from last run before save_memory was removed from core/memory.py. bench.py §3 is currently broken — pending save_memory tool review in BACKLOG."
        />

        <H3>4 — SQLite History</H3>
        <BenchTable rows={sqliteRows} />
      </Stack>

      <Divider />

      {/* Live LLM baseline */}
      <Stack gap={12}>
        <Stack gap={2}>
          <H2>Live LLM Baseline</H2>
          <Text tone="secondary" size="small">
            Run <Code>bench.py --live</Code> for each provider and fill in results. Hard limit: TTFT &lt; 3000 ms.
          </Text>
        </Stack>

        <Callout tone="info" title="Vertex Claude baseline recorded — Gemini and direct Anthropic still TBD">
          <Text size="small">
            Run <Code>uv run python testing/bench.py --live</Code> with <Code>MODEL_PROVIDER=gemini</Code> or <Code>MODEL_PROVIDER=claude</Code> to fill in remaining rows. GCP creds are auto-loaded; Gemini and Anthropic direct require their own API keys.
          </Text>
        </Callout>

        <Table
          headers={["Provider", "Model", "TTFT cold", "TTFT warm", "e2e cold / warm"]}
          rows={liveLlmRows.map(([provider, model, ttft_cold, ttft_warm, e2e]) => [
            provider,
            model,
            <StatusPill key={provider + "cold"} status={ttft_cold} />,
            <StatusPill key={provider + "warm"} status={ttft_warm} />,
            <StatusPill key={provider + "e2e"} status={e2e} />,
          ])}
        />
      </Stack>

      <Divider />

      {/* Docker baseline */}
      <Stack gap={12}>
        <Stack gap={2}>
          <H2>Docker / HTTP Baseline</H2>
          <Text tone="secondary" size="small">
            Run <Code>bench.py --docker</Code> with the container up. These are HTTP response times, not container boot time.
          </Text>
        </Stack>
        <BenchTable
          rows={dockerRows}
          note="Requires: docker compose up -d first, then bench.py --docker"
        />
      </Stack>

      <Divider />

      {/* Container cold start */}
      <Stack gap={10}>
        <H2>Container Cold Start — Not Yet Measured</H2>
        <Text tone="secondary" size="small">
          <Code>bench.py --docker</Code> only probes HTTP once the container is already running.
          Cold boot time requires a separate measurement.
        </Text>
        <Card>
          <CardHeader>How to measure container boot time</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Code>docker compose -f docker/docker-compose.yml down</Code>
              <Code>time docker compose -f docker/docker-compose.yml up --wait</Code>
              <Text size="small" tone="secondary">
                Record the wall time from <Code>time</Code>. Suggested target: &lt; 30 s on warm image cache.
                Run this 3 times and take the average — first run includes image layer resolution.
              </Text>
            </Stack>
          </CardBody>
        </Card>
      </Stack>

      <Divider />

      {/* Cross-session memory */}
      <Stack gap={10}>
        <H2>Cross-Session Memory Persistence — Not Yet Automated</H2>
        <Text tone="secondary" size="small">
          No pytest covers process-restart → memory reload. Manual runbook until a test is added to
          <Code>tests/integration/</Code>.
        </Text>
        <Card>
          <CardHeader>Manual test runbook</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text size="small"><strong>Step 1</strong> — Start a session:</Text>
              <Code>uv run monkeybot run --bot-dir ../sandbox/bots/devbot</Code>
              <Text size="small"><strong>Step 2</strong> — Ask the agent to save something specific:</Text>
              <Code>"Remember: the magic word is BANANA"</Code>
              <Text size="small"><strong>Step 3</strong> — Exit (Ctrl+C), then start a new session:</Text>
              <Code>uv run monkeybot run --bot-dir ../sandbox/bots/devbot</Code>
              <Text size="small"><strong>Step 4</strong> — Verify recall:</Text>
              <Code>"What is the magic word?"</Code>
              <Text size="small" tone="secondary">
                Expected: agent recalls BANANA from the memory index without re-prompting.
                If it hallucinates or says it doesn't know — memory persistence is broken.
              </Text>
            </Stack>
          </CardBody>
        </Card>
      </Stack>

      <Divider />

      {/* Feature coverage */}
      <Stack gap={12}>
        <H2>Feature Test Coverage</H2>
        <Table
          headers={["Feature", "Location", "Status"]}
          rows={featureRows.map(([feature, location, status]) => [
            feature,
            <Text key={feature} size="small" tone="secondary">{location}</Text>,
            <StatusPill key={feature + status} status={status} />,
          ])}
        />
      </Stack>

      <Divider />

      {/* Success criteria */}
      <Stack gap={12}>
        <H2>Target Success Criteria</H2>
        <Text tone="secondary" size="small">Design goals from monkeybot_v2_plan.md — independent of bench.py hard limits.</Text>
        <Table
          headers={["Metric", "Target", "Current"]}
          rows={criteriaRows.map(([metric, target, current]) => [
            metric,
            target,
            <StatusPill key={metric} status={current} />,
          ])}
        />
      </Stack>

      <Divider />

      {/* How to update */}
      <Stack gap={10}>
        <H2>How to Update This Baseline</H2>
        <Grid columns={2} gap={12}>
          <Stack gap={6}>
            <H3>Steps</H3>
            <Text size="small">1. Run the full suite:</Text>
            <Code>uv run python testing/bench.py --live --docker && uv run pytest tests/ -v</Code>
            <Text size="small">2. Copy new numbers into <Code>testing/BENCH_NOTES.md</Code></Text>
            <Text size="small">3. Update the date in the offline section header</Text>
            <Text size="small">4. Regenerate this canvas (or ask Cursor to update it)</Text>
            <Text size="small">5. Commit both together:</Text>
            <Code>git add testing/ && git commit -m "update baseline YYYY-MM-DD"</Code>
          </Stack>
          <Stack gap={6}>
            <H3>When to re-baseline</H3>
            <Text size="small">— Provider upgraded or swapped</Text>
            <Text size="small">— New dependency added (cold start may regress)</Text>
            <Text size="small">— Memory or history implementation changes</Text>
            <Text size="small">— Any bench section starts failing in CI</Text>
            <Text size="small">— Before/after a performance-sensitive refactor</Text>
            <Text size="small">— New team member onboarding (give them a clean run to compare)</Text>
          </Stack>
        </Grid>
      </Stack>

      <Divider />

      <Text tone="secondary" size="small">
        Benchmark runner: <Code>testing/bench.py</Code> — Pytest: <Code>tests/</Code> — Full notes: <Code>testing/BENCH_NOTES.md</Code>
      </Text>

    </Stack>
  );
}
