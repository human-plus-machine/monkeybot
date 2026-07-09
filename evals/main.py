"""FastAPI evals service (T3) — drives the live gateway, scores with deepeval, optional Langfuse."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from assertions import evaluate_assertions
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from models import (
    CreateRunRequest,
    CreateRunResponse,
    CreateScenarioBody,
    EvalRun,
    HealthResponse,
    Scenario,
    TurnResult,
)
from runner import run_scenario_live
from scenario_loader import load_disk_scenarios, parse_scenario_yaml
from scorer import push_langfuse_scores, score_scenario
from store import EvalStore
from ws_manager import WSManager

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = EvalStore()
    store.replace_scenarios(load_disk_scenarios())
    app.state.store = store
    app.state.ws = WSManager()
    app.state.agent_url = os.environ.get("AGENT_URL", "http://127.0.0.1:8080").rstrip("/")
    _log.info("Evals service started; agent_url=%s", app.state.agent_url)
    yield


app = FastAPI(title="monkeybot Evals", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _execute_run(app: FastAPI, run_id: str, scenario: Scenario) -> None:
    store: EvalStore = app.state.store
    ws: WSManager = app.state.ws
    agent_url: str = app.state.agent_url

    async def broadcast(msg_type: str, payload: dict[str, Any]) -> None:
        await ws.broadcast(run_id, {"type": msg_type, "payload": payload})

    def patch_run(**kwargs: Any) -> None:
        cur = store.get_run(run_id)
        if not cur:
            return
        store.put_run(cur.model_copy(update=kwargs))

    try:
        await broadcast("status", {"status": "running"})

        async def on_turn(_idx: int, tr: TurnResult) -> None:
            cur = store.get_run(run_id)
            if not cur:
                return
            new_turns = list(cur.turns) + [tr]
            store.put_run(cur.model_copy(update={"turns": new_turns}))
            await broadcast(
                "turn",
                {
                    "turn_index": len(new_turns) - 1,
                    "input": tr.input,
                    "output": tr.output,
                    "trace_id": tr.trace_id,
                },
            )

        turns_final = await run_scenario_live(scenario, agent_url, on_turn_complete=on_turn)

        scores, details, pass_rate = await asyncio.to_thread(score_scenario, scenario, turns_final)
        await asyncio.to_thread(push_langfuse_scores, turns_final, scores, pass_rate)

        run_for_assertions = EvalRun(
            run_id=run_id, scenario_id=scenario.id, turns=turns_final, scores=scores
        )
        requirement_failures = evaluate_assertions(scenario, run_for_assertions)

        patch_run(
            turns=turns_final,
            scores=scores,
            score_details=details,
            pass_rate=pass_rate,
            status="failed" if requirement_failures else "completed",
            error=None,
            requirement_failures=requirement_failures,
        )
        await broadcast(
            "scores",
            {
                "scores": scores,
                "pass_rate": pass_rate,
                "details": details,
                "requirement_failures": requirement_failures,
            },
        )
        await broadcast("status", {"status": "completed"})
    except Exception as exc:
        _log.exception("Run %s failed", run_id)
        cur = store.get_run(run_id)
        if cur:
            store.put_run(cur.model_copy(update={"status": "failed", "error": str(exc)}))
        await broadcast("error", {"message": str(exc)})
        await broadcast("status", {"status": "failed"})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/api/runs", response_model=CreateRunResponse)
async def create_run(req: CreateRunRequest) -> CreateRunResponse:
    store: EvalStore = app.state.store
    sid = req.scenario_id
    if not sid and req.scenario_ids:
        if len(req.scenario_ids) != 1:
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one scenario via scenario_id or a single-element scenario_ids",
            )
        sid = req.scenario_ids[0]
    if not sid:
        raise HTTPException(status_code=400, detail="scenario_id is required")
    scenario = store.get_scenario(sid)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"Unknown scenario {sid}")
    run_id = str(uuid.uuid4())
    run = EvalRun(run_id=run_id, scenario_id=sid, status="running", created_at=_utc_now_iso())
    store.put_run(run)
    asyncio.create_task(_execute_run(app, run_id, scenario.model_copy(deep=True)))
    return CreateRunResponse(run_id=run_id)


@app.get("/api/runs", response_model=list[EvalRun])
def list_runs() -> list[EvalRun]:
    store: EvalStore = app.state.store
    runs = store.list_runs()
    runs.sort(key=lambda r: r.created_at or "", reverse=True)
    return runs


@app.get("/api/runs/{run_id}", response_model=EvalRun)
def get_run(run_id: str) -> EvalRun:
    store: EvalStore = app.state.store
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str) -> None:
    store: EvalStore = app.state.store
    if not store.delete_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")


@app.get("/api/scenarios", response_model=list[Scenario])
def list_scenarios() -> list[Scenario]:
    store: EvalStore = app.state.store
    return store.list_scenarios()


@app.get("/api/scenarios/{scenario_id:path}", response_model=Scenario)
def get_scenario(scenario_id: str) -> Scenario:
    store: EvalStore = app.state.store
    sc = store.get_scenario(scenario_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return sc


@app.post("/api/scenarios", response_model=Scenario)
def create_scenario(body: CreateScenarioBody) -> Scenario:
    store: EvalStore = app.state.store
    try:
        scenario = parse_scenario_yaml(body.yaml, sid=None, source="operator")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing = store.get_scenario(scenario.id)
    if existing is not None:
        if existing.source == "builtin":
            raise HTTPException(status_code=409, detail="Scenario id conflicts with a built-in scenario")
        raise HTTPException(status_code=409, detail="Scenario id already exists; use PUT to replace")
    store.upsert_scenario(scenario)
    return scenario


@app.put("/api/scenarios/{scenario_id:path}", response_model=Scenario)
def put_scenario(scenario_id: str, body: CreateScenarioBody) -> Scenario:
    store: EvalStore = app.state.store
    existing = store.get_scenario(scenario_id)
    if existing is not None and existing.source == "builtin":
        raise HTTPException(status_code=403, detail="Built-in scenarios cannot be updated")
    try:
        scenario = parse_scenario_yaml(body.yaml, sid=scenario_id, source="operator")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if scenario.id != scenario_id:
        raise HTTPException(status_code=400, detail="YAML id/filename must match URL scenario id")
    store.upsert_scenario(scenario)
    return scenario


@app.delete("/api/scenarios/{scenario_id:path}", status_code=204)
def delete_scenario(scenario_id: str) -> None:
    store: EvalStore = app.state.store
    existing = store.get_scenario(scenario_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if existing.source == "builtin":
        raise HTTPException(status_code=403, detail="Built-in scenarios cannot be deleted")
    if not store.delete_scenario(scenario_id):
        raise HTTPException(status_code=404, detail="Scenario not found")


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str) -> None:
    mgr: WSManager = app.state.ws
    store: EvalStore = app.state.store
    await mgr.connect(run_id, ws)
    # Replay current run so clients that connect after a fast failure still see terminal state.
    cur = store.get_run(run_id)
    if cur is not None:
        snap = {"type": "snapshot", "payload": cur.model_dump(mode="json")}
        await ws.send_text(json.dumps(snap, default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await mgr.disconnect(run_id, ws)
