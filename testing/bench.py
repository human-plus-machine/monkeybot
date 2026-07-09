#!/usr/bin/env python3
"""
monkeybot v2 — Sandbox Benchmark Suite
=======================================
Measures: cold start, time-to-first-token (TTFT), e2e turn latency,
tool dispatch overhead, memory ops, and streaming throughput.

Usage (run from monkeybot/ repo root):
  uv run python testing/bench.py                  # cold start only (no API key needed)
  uv run python testing/bench.py --live           # + live LLM via Vertex Claude (auto-loads GCP creds)
  uv run python testing/bench.py --docker         # + HTTP tests against http://localhost:8080
  uv run python testing/bench.py --live --docker  # everything

  Default provider: vertex-claude (gcp_service_auth_qa.json loaded automatically).
  Override: MODEL_PROVIDER=gemini|claude|vertex-claude  MODEL=<model-id>
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── resolve paths ─────────────────────────────────────────────────────────────
TESTING_DIR = Path(__file__).parent
MONKEYBOT_ROOT = TESTING_DIR.parent
MONKEYBOT_SRC = MONKEYBOT_ROOT / "src"
BOT_DIR = TESTING_DIR / "bots" / "devbot"
SKILLS_PATH = str(MONKEYBOT_ROOT / ".agents" / "skills")

# Make monkeybot importable without installing it
if str(MONKEYBOT_SRC) not in sys.path:
    sys.path.insert(0, str(MONKEYBOT_SRC))

# ── Vertex AI: auto-read project ID from GOOGLE_APPLICATION_CREDENTIALS ───────
# Set GOOGLE_APPLICATION_CREDENTIALS to your service account JSON before running.
# ANTHROPIC_VERTEX_PROJECT_ID is extracted automatically if not already set.
_gcp_creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
if _gcp_creds_path and not os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
    try:
        import json as _json
        _svc = _json.loads(Path(_gcp_creds_path).read_text())
        _pid = _svc.get("project_id") or _svc.get("quota_project_id")
        if _pid:
            os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] = _pid
    except Exception:
        pass


# ── helpers ───────────────────────────────────────────────────────────────────
def _default_model(provider: str) -> str:
    return {
        "gemini": "gemini-2.0-flash",
        "claude": "claude-sonnet-4-6@default",
        "vertex-claude": "claude-sonnet-4-6@default",
    }.get(provider, "gemini-2.0-flash")
class Result:
    def __init__(self, name: str, value_ms: float, passed: bool, limit_ms: float | None = None, note: str = ""):
        self.name = name
        self.value_ms = value_ms
        self.passed = passed
        self.limit_ms = limit_ms
        self.note = note


results: list[Result] = []


def record(name: str, value_ms: float, limit_ms: float | None = None, note: str = "") -> Result:
    passed = value_ms < limit_ms if limit_ms is not None else True
    r = Result(name, value_ms, passed, limit_ms, note)
    results.append(r)
    status = "PASS" if passed else "FAIL"
    limit_str = f" (limit {limit_ms:.0f}ms)" if limit_ms else ""
    print(f"  {'✓' if passed else '✗'} [{status}] {name}: {value_ms:.1f}ms{limit_str}{' — ' + note if note else ''}")
    return r


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def run_subprocess(cmd: list[str], timeout: int = 15) -> tuple[int, float, str]:
    """Run a subprocess, return (returncode, elapsed_ms, stderr)."""
    start = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(MONKEYBOT_ROOT))
    elapsed_ms = (time.monotonic() - start) * 1000
    return result.returncode, elapsed_ms, result.stderr.decode()


# ── 1. Cold Start Tests ───────────────────────────────────────────────────────
def bench_cold_start() -> None:
    section("1. COLD START")

    # 1a. Raw import time (no uv startup overhead)
    code = "import monkeybot"
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(MONKEYBOT_SRC)},
    )
    import_ms = (time.monotonic() - start) * 1000
    if proc.returncode != 0:
        print(f"  ✗ [FAIL] import monkeybot: ERROR — {proc.stderr.decode()[:200]}")
    else:
        record("import monkeybot", import_ms, limit_ms=2000, note="includes python startup")

    # 1b. CLI startup — monkeybot --help (skipped until CLI entry point is added to pyproject.toml)
    rc, cli_ms, err = run_subprocess(["uv", "run", "monkeybot", "--help"])
    if rc != 0:
        print(f"  ⚠  [SKIP] monkeybot --help: CLI not installed — {err.strip()[:120]}")
        print("           Add [project.scripts] monkeybot = ... to pyproject.toml to enable this test.")
    else:
        record("monkeybot --help (uv)", cli_ms, limit_ms=5000, note="includes uv startup")

    # 1c. Submodule import: core only
    core_code = "from monkeybot.core.runtime.loop import AgentLoop"
    start = time.monotonic()
    subprocess.run(
        [sys.executable, "-c", core_code],
        capture_output=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(MONKEYBOT_SRC)},
    )
    core_ms = (time.monotonic() - start) * 1000
    record("import AgentLoop (core only)", core_ms, limit_ms=1500, note="includes python startup")

    # 1d. Provider import (lazy — should not pull in heavy SDK at top level)
    provider_code = "from monkeybot.providers.gemini import GeminiProvider"
    start = time.monotonic()
    subprocess.run(
        [sys.executable, "-c", provider_code],
        capture_output=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(MONKEYBOT_SRC)},
    )
    provider_ms = (time.monotonic() - start) * 1000
    record("import GeminiProvider", provider_ms, limit_ms=2000)


# ── 2. In-Process Harness Benchmarks (no LLM) ────────────────────────────────
async def bench_harness() -> None:
    section("2. HARNESS OVERHEAD (fake provider, no LLM)")

    from monkeybot.core.context import TurnContext
    from monkeybot.core.runtime.events import AssistantDelta, TurnComplete
    from monkeybot.core.persistence.sqlite import apply_schema, open_connection
    from monkeybot.core.testing.mocks_provider import fake_provider_prompt_tokens
    from monkeybot.core.persistence.history import SQLiteHistoryStore
    from monkeybot.core.runtime.loop import run
    from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
    from monkeybot.core.types.types_tools import ToolDef

    class FakeProvider:
        name = "fake"
        supports_streaming = True

        def __init__(self, events: list[Any]) -> None:
            self._events = events

        async def stream(self, messages: Any, tools: Any, *, model: str) -> Any:  # type: ignore[override]
            for ev in self._events:
                yield ev

        async def count_input_tokens(self, messages: Any, tools: Any, *, model: str) -> int:
            del model
            return fake_provider_prompt_tokens(messages, tools)

    class NoopExecutor:
        async def execute(self, *, call: Any, ctx: Any) -> tuple[str | None, str | None]:
            return ("ok", None)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        agent_md = tmp_path / "AGENT.md"
        agent_md.write_text("# DevBot\nYou are a test bot.")
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        conn = await open_connection(f"sqlite:///{tmp_path}/bench.db")
        await apply_schema(conn)
        history = SQLiteHistoryStore(conn)
        executor = NoopExecutor()

        def _ctx(request_id: str) -> TurnContext:
            return TurnContext(
                thread_id="bench",
                request_id=request_id,
                agent_md="# DevBot\nYou are a test bot.",
                memory_index=[],
                skills=[],
                tools=[ToolDef("run_command", "Run shell", {})],
                user_id=None,
                parent_run_id=None,
                model="fake",
            )

        # 2a. First-turn latency (DB init already done; measures run() overhead)
        prov_init = FakeProvider([
            TextDelta(text="Hello!"),
            UsageEvent(input_tokens=20, output_tokens=5, cached_tokens=0),
            Done(),
        ])
        start = time.monotonic()
        async for _ in run("hi", _ctx("bench-init"), provider=prov_init, history=history, inspectors=[], tool_executor=executor):
            pass
        init_ms = (time.monotonic() - start) * 1000
        record("first turn (DB init included)", init_ms, limit_ms=500)

        # 2b. TTFT — time from run() call to first AssistantDelta
        prov_ttft = FakeProvider([
            TextDelta(text="A"),
            TextDelta(text="B"),
            TextDelta(text="C"),
            UsageEvent(input_tokens=10, output_tokens=3, cached_tokens=0),
            Done(),
        ])
        ttft_ms: float = 0.0
        t0 = time.monotonic()
        async for ev in run("ping", _ctx("bench-ttft"), provider=prov_ttft, history=history, inspectors=[], tool_executor=executor):
            if isinstance(ev, AssistantDelta) and ttft_ms == 0.0:
                ttft_ms = (time.monotonic() - t0) * 1000
        record("TTFT (fake provider)", ttft_ms, limit_ms=100, note="harness overhead only")

        # 2c. Warm turn latency (×5)
        WARM_RUNS = 5
        warm_times: list[float] = []
        for i in range(WARM_RUNS):
            prov_w = FakeProvider([
                TextDelta(text="hi"),
                UsageEvent(input_tokens=10, output_tokens=2, cached_tokens=0),
                Done(),
            ])
            t = time.monotonic()
            async for _ in run(f"msg {i}", _ctx(f"bench-warm-{i}"), provider=prov_w, history=history, inspectors=[], tool_executor=executor):
                pass
            warm_times.append((time.monotonic() - t) * 1000)
        avg_warm = sum(warm_times) / len(warm_times)
        record(f"warm turn avg ({WARM_RUNS} runs)", avg_warm, limit_ms=200, note=f"min={min(warm_times):.1f}ms max={max(warm_times):.1f}ms")

        # 2d. Streaming throughput — 100 tokens
        N_TOKENS = 100
        prov_thr = FakeProvider(
            [TextDelta(text="x") for _ in range(N_TOKENS)]
            + [UsageEvent(input_tokens=10, output_tokens=N_TOKENS, cached_tokens=0), Done()]
        )
        t_start = time.monotonic()
        token_count = 0
        async for ev in run("stream", _ctx("bench-thr"), provider=prov_thr, history=history, inspectors=[], tool_executor=executor):
            if isinstance(ev, AssistantDelta):
                token_count += 1
        thr_ms = (time.monotonic() - t_start) * 1000
        toks_per_sec = token_count / (thr_ms / 1000) if thr_ms > 0 else 0
        record("streaming throughput", thr_ms, note=f"{token_count} tokens @ {toks_per_sec:,.0f} tok/s (harness only)")

        await conn.close()


# ── 3. Memory Ops ─────────────────────────────────────────────────────────────
async def bench_memory() -> None:
    section("3. MEMORY OPS")

    from monkeybot.core.memory import async_search_memory_files
    from monkeybot.core.workspace import create_workspace_storage

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 3a. Write 10 files directly (save_memory removed from API; write_file tool is the path)
        WRITE_N = 10
        t = time.monotonic()
        for i in range(WRITE_N):
            (tmp_path / f"note-{i}.md").write_text(f"# Note {i}\n\nContent for note {i}. " * 20)
        write_ms = (time.monotonic() - t) * 1000
        record(f"write {WRITE_N} memory files", write_ms, limit_ms=200, note=f"{write_ms/WRITE_N:.1f}ms each")

        # 3b. Keyword search over all files
        storage = create_workspace_storage("local://" + str(tmp_path.resolve()))
        t = time.monotonic()
        payload = await async_search_memory_files(storage, "Content for note 5", max_hits=20)
        results = [h.get("path", "") for h in payload.get("hits", []) if isinstance(h, dict)]
        search_ms = (time.monotonic() - t) * 1000
        found = any("note-5" in r for r in results)
        record("search_memory_files (10 files)", search_ms, limit_ms=50, note=f"{'hit' if found else 'miss'}")

        # 3c. Direct file read (baseline for read_file tool)
        t = time.monotonic()
        _ = (tmp_path / "note-0.md").read_text()
        read_ms = (time.monotonic() - t) * 1000
        record("read memory file (direct)", read_ms, limit_ms=20)


# ── 4. Conversation History (SQLite) ──────────────────────────────────────────
async def bench_history() -> None:
    section("4. CONVERSATION HISTORY (SQLite)")

    from monkeybot.core.persistence.sqlite import apply_schema, open_connection
    from monkeybot.core.persistence.history import SQLiteHistoryStore
    from monkeybot.core.llm.provider import Message

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = await open_connection(f"sqlite:///{tmp}/history.db")
        await apply_schema(conn)
        db = SQLiteHistoryStore(conn)

        # 4a. Append 20 messages
        t = time.monotonic()
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            await db.append(
                "bench-session",
                Message.text(role, f"message {i}"),
            )
        save_ms = (time.monotonic() - t) * 1000
        record("save 20 messages (SQLite)", save_ms, limit_ms=500)

        # 4b. Load history
        t = time.monotonic()
        msgs = await db.load("bench-session")
        load_ms = (time.monotonic() - t) * 1000
        record("load history (20 msgs)", load_ms, limit_ms=50, note=f"{len(msgs)} messages")

        await conn.close()


# ── 5. Live LLM Tests ────────────────────────────────────────────────────────
def _load_live_provider(provider_name: str) -> Any:
    """Load a provider and return it, or raise with a clear message if misconfigured."""
    if provider_name == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("GEMINI_API_KEY not set")
        from monkeybot.providers.gemini import GeminiProvider  # noqa: PLC0415
        return GeminiProvider()
    if provider_name == "claude":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from monkeybot.providers.claude import ClaudeProvider  # noqa: PLC0415
        return ClaudeProvider()
    if provider_name == "vertex-claude":
        if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS not set.\n"
                "  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json\n"
                "  ANTHROPIC_VERTEX_PROJECT_ID is read from the file automatically."
            )
        if not os.getenv("ANTHROPIC_VERTEX_PROJECT_ID"):
            raise RuntimeError(
                "ANTHROPIC_VERTEX_PROJECT_ID not set and could not be read from credentials file.\n"
                "  export ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id"
            )
        from monkeybot.providers.vertex_claude import VertexClaudeProvider  # noqa: PLC0415
        return VertexClaudeProvider()
    raise RuntimeError(f"Unknown MODEL_PROVIDER: {provider_name}. Supported: gemini, claude, vertex-claude")


async def bench_live() -> None:
    section("5. LIVE LLM — Time to First Token")

    provider_name = os.getenv("MODEL_PROVIDER", "vertex-claude")
    model = os.getenv("MODEL", _default_model(provider_name))

    try:
        provider = _load_live_provider(provider_name)
    except RuntimeError as e:
        print(f"  ⚠  Skipping — {e}")
        return

    from monkeybot.core.context import TurnContext
    from monkeybot.core.persistence.sqlite import apply_schema, open_connection
    from monkeybot.core.runtime.events import AssistantDelta, Error, TurnComplete
    from monkeybot.core.persistence.history import SQLiteHistoryStore
    from monkeybot.core.runtime.loop import run

    class NoopExecutor:
        async def execute(self, *, call: Any, ctx: Any) -> tuple[str | None, str | None]:
            return ("ok", None)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        conn = await open_connection(f"sqlite:///{tmp_path}/live.db")
        await apply_schema(conn)
        history = SQLiteHistoryStore(conn)
        executor = NoopExecutor()

        def _ctx(request_id: str) -> TurnContext:
            return TurnContext(
                thread_id="bench-live",
                request_id=request_id,
                agent_md="# DevBot\nYou are DevBot. Respond in exactly 5 words.",
                memory_index=[],
                skills=[],
                tools=[],  # no tools — TTFT only, avoids schema validation on provider side
                user_id=None,
                parent_run_id=None,
                model=model,
            )

        # 5a. TTFT — real network call (cold)
        ttft_ms: float = 0.0
        e2e_ms: float = 0.0
        full_text = ""
        print(f"  [{provider_name} / {model}] First prompt (cold)...", end=" ", flush=True)
        t0 = time.monotonic()
        async for ev in run("Say hello in exactly 5 words.", _ctx("bench-live-1"), provider=provider, history=history, inspectors=[], tool_executor=executor):
            if isinstance(ev, AssistantDelta):
                full_text += ev.delta
                if ttft_ms == 0.0:
                    ttft_ms = (time.monotonic() - t0) * 1000
            elif isinstance(ev, Error):
                print(f"\n  ✗ [FAIL] Provider error: {ev.error}")
                return
            elif isinstance(ev, TurnComplete):
                e2e_ms = (time.monotonic() - t0) * 1000
        print("done")
        record("TTFT (live, cold)", ttft_ms, limit_ms=3000, note=f"{provider_name}/{model}")
        record("e2e turn latency (live, cold)", e2e_ms, note=f'"{full_text.strip()[:60]}"')

        # 5b. Second turn (warm — session history already loaded)
        print(f"  [{provider_name} / {model}] Second prompt (warm)...", end=" ", flush=True)
        ttft2: float = 0.0
        e2e2: float = 0.0
        t1 = time.monotonic()
        async for ev in run("What did I just ask?", _ctx("bench-live-2"), provider=provider, history=history, inspectors=[], tool_executor=executor):
            if isinstance(ev, AssistantDelta) and ttft2 == 0.0:
                ttft2 = (time.monotonic() - t1) * 1000
            elif isinstance(ev, Error):
                print(f"\n  ✗ [FAIL] Provider error: {ev.error}")
                break
            elif isinstance(ev, TurnComplete):
                e2e2 = (time.monotonic() - t1) * 1000
        print("done")
        record("TTFT (live, warm session)", ttft2, limit_ms=3000)
        record("e2e turn latency (live, warm)", e2e2)

        await conn.close()


# ── 6. Docker / HTTP Tests ────────────────────────────────────────────────────
async def bench_docker(base_url: str = "http://localhost:8080") -> None:
    section(f"6. DOCKER / HTTP — {base_url}")

    try:
        import httpx
    except ImportError:
        print("  ⚠  httpx not installed — skipping Docker tests")
        return

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # 6a. /health cold check
        try:
            t = time.monotonic()
            resp = await client.get("/health")
            health_ms = (time.monotonic() - t) * 1000
            if resp.status_code == 200:
                record("/health response", health_ms, limit_ms=500)
            else:
                print(f"  ✗ [FAIL] /health returned {resp.status_code}")
        except Exception as e:
            print(f"  ✗ [FAIL] /health unreachable — {e}")
            print("  ⚠  Is Docker running? Try: docker compose -f monkeybot/docker/docker-compose.yml up")
            return

        # 6b. Webhook TTFT (streaming)
        import json

        payload = {"message": "Say hi in 5 words.", "user_id": "bench"}
        print("  Sending webhook request...", end=" ", flush=True)
        t = time.monotonic()
        try:
            resp = await client.post("/webhook", json=payload)
            webhook_ms = (time.monotonic() - t) * 1000
            if resp.status_code == 200:
                record("/webhook e2e", webhook_ms, limit_ms=10000, note=f"status={resp.status_code}")
            else:
                print(f"\n  ✗ [FAIL] /webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"\n  ✗ [FAIL] /webhook error — {e}")

        # 6c. Concurrent requests
        CONCURRENCY = 3
        print(f"  Firing {CONCURRENCY} concurrent requests...", end=" ", flush=True)
        t = time.monotonic()
        tasks = [client.post("/webhook", json={"message": f"Say hi {i}", "user_id": f"u{i}"}) for i in range(CONCURRENCY)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        concurrent_ms = (time.monotonic() - t) * 1000
        ok = sum(1 for r in responses if hasattr(r, "status_code") and r.status_code == 200)
        record(f"{CONCURRENCY}x concurrent /webhook", concurrent_ms, note=f"{ok}/{CONCURRENCY} succeeded")


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary() -> None:
    print(f"\n{'═' * 60}")
    print("  SUMMARY")
    print(f"{'═' * 60}")
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    print(f"  Tests run: {len(results)}  |  Passed: {len(passed)}  |  Failed: {len(failed)}")
    if failed:
        print(f"\n  FAILURES:")
        for r in failed:
            limit_str = f" (limit was {r.limit_ms:.0f}ms)" if r.limit_ms else ""
            print(f"    ✗ {r.name}: {r.value_ms:.1f}ms{limit_str}")
    print(f"{'═' * 60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────
async def main_async(args: argparse.Namespace) -> None:
    provider_name = os.getenv("MODEL_PROVIDER", "vertex-claude")
    model = os.getenv("MODEL", _default_model(provider_name))
    print("\n  monkeybot v2 Sandbox Benchmark")
    print(f"  Provider : {provider_name}  |  Model: {model}")
    print(f"  Bot      : {BOT_DIR}")
    print(f"  Skills   : {SKILLS_PATH}")

    bench_cold_start()
    await bench_harness()
    await bench_memory()
    await bench_history()

    if args.live:
        await bench_live()

    if args.docker:
        await bench_docker(args.docker_url)

    print_summary()

    failed = [r for r in results if not r.passed]
    sys.exit(1 if failed else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="monkeybot v2 Benchmark Suite")
    parser.add_argument("--live", action="store_true", help="Run live LLM tests (needs API key)")
    parser.add_argument("--docker", action="store_true", help="Run HTTP tests against Docker")
    parser.add_argument("--docker-url", default="http://localhost:8080", help="Docker base URL (default: http://localhost:8080)")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
