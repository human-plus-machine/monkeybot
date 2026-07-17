"""Tests for auriga answer scorer + recall rank report."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[2]
_EVAL = _REPO / "evals" / "knowledge_layer"


def _load(name: str, filename: str) -> ModuleType:
    path = _EVAL / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_EVAL))
    try:
        spec.loader.exec_module(mod)
    finally:
        if sys.path and sys.path[0] == str(_EVAL):
            sys.path.pop(0)
    return mod


@pytest.fixture(scope="module")
def score_mod() -> ModuleType:
    return _load("score_auriga_answers", "score_auriga_answers.py")


@pytest.fixture(scope="module")
def rank_mod() -> ModuleType:
    return _load("recall_rank_report", "recall_rank_report.py")


def test_parse_answers_plain_and_heading(score_mod: ModuleType) -> None:
    text = """
Q01: fifteen minutes FERPA
## Q02: browserLocalPersistence
### Q03: some key answer
"""
    answers = score_mod.parse_answers(text)
    assert answers["Q01"].startswith("fifteen")
    assert "browserLocalPersistence" in answers["Q02"]
    assert "some key" in answers["Q03"]


def test_accept_grouped_or_and(score_mod: ModuleType) -> None:
    hay = "uses bearer token with x-tenant-id header"
    assert score_mod.eval_accept_expr(
        "`getAgentGatewayAuthHeaders` OR (`Bearer` AND `X-Tenant-Id`)",
        hay,
    )
    assert not score_mod.eval_accept_expr(
        "`getAgentGatewayAuthHeaders` OR (`Bearer` AND `X-Tenant-Id`)",
        "only bearer missing tenant",
    )
    assert score_mod.eval_accept_expr(
        "`LangGraph` AND (`Agent Engine` OR `useAgentStream` OR `MonkeyBot`)",
        "langgraph via monkeybot runtime",
    )


def test_strip_emphasis_helps_accept(score_mod: ModuleType) -> None:
    fields = {"accept": "`15`, `FERPA`"}
    ok, reason = score_mod.score_one("**15** minutes for **FERPA**", fields)
    assert ok
    assert reason == "accept"


def test_all_flag_lists_every_qid(score_mod: ModuleType, tmp_path: Path) -> None:
    qa = tmp_path / "qa.md"
    qa.write_text(
        "### Q01\n- **Question:** a\n- **Answer:** yes\n- **Accept:** `yes`\n\n"
        "### Q02\n- **Question:** b\n- **Answer:** no\n- **Accept:** `no`\n",
        encoding="utf-8",
    )
    answers = tmp_path / "ans.md"
    answers.write_text("## Q01: yes\n## Q02: no\n", encoding="utf-8")
    rc = score_mod.main(["--answers", str(answers), "--qa-file", str(qa), "--all"])
    assert rc == 0


def test_recall_rank_report_metrics(rank_mod: ModuleType, tmp_path: Path) -> None:
    qa = tmp_path / "qa.md"
    qa.write_text(
        "### Q01\n- **Question:** idle?\n- **Answer:** 15\n"
        "- **Evidence:** `src/auth.ts`\n- **Accept:** `15`\n\n"
        "### Q02\n- **Question:** firebase?\n- **Answer:** local\n"
        "- **Evidence:** `src/lib/firebase.ts`\n- **Accept:** `local`\n",
        encoding="utf-8",
    )
    recall_payload = {
        "ok": True,
        "hits": [
            {"path": "src/other.ts", "score": 1.0},
            {"path": "src/auth.ts", "score": 0.8},
            {"path": "src/lib/firebase.ts", "score": 0.5},
        ],
    }
    records = [
        {"seq": 1, "type": "UserMessage", "content": "ask all"},
        {
            "seq": 10,
            "type": "ToolCallResult",
            "tool": "recall",
            "result": json.dumps(recall_payload),
        },
        {
            "seq": 11,
            "type": "ToolCallStarted",
            "tool": "read_file",
            "args": {"path": "src/auth.ts"},
        },
        {
            "seq": 12,
            "type": "ToolCallResult",
            "tool": "read_file",
            "result": "ok",
        },
        {"seq": 50, "type": "ContextSummarized", "summary": "compressed"},
        {
            "seq": 60,
            "type": "ToolCallResult",
            "tool": "recall",
            "result": json.dumps(
                {"ok": True, "hits": [{"path": "src/lib/firebase.ts", "score": 1.0}]}
            ),
        },
    ]
    tpath = tmp_path / "transcript.ndjson"
    tpath.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    qa_map = rank_mod.parse_qa(qa.read_text(encoding="utf-8"))
    loaded = rank_mod.load_ndjson(tpath)
    ranks = rank_mod.best_evidence_ranks(loaded, qa_map)
    assert ranks["Q01"] == 2
    assert ranks["Q02"] == 1
    assert rank_mod.recall_at_k(ranks, 1) == pytest.approx(0.5)
    assert rank_mod.recall_at_k(ranks, 3) == pytest.approx(1.0)
    assert rank_mod.mean_reciprocal_rank(ranks) == pytest.approx((1 / 2 + 1 / 1) / 2)

    rate, verified, total = rank_mod.verification_rate(loaded, window=8)
    assert total == 2
    assert verified >= 1
    assert rate > 0

    endurance = rank_mod.endurance_stats(loaded)
    assert endurance["summarizations"] == 1
    assert endurance["recall_total"] == 2

    rc = rank_mod.main(["--transcript", str(tpath), "--qa-file", str(qa)])
    assert rc == 0


def test_missing_evidence_is_inf(rank_mod: ModuleType) -> None:
    ranks = {"Q01": math.inf, "Q02": 4.0}
    assert rank_mod.recall_at_k(ranks, 3) == pytest.approx(0.0)
    assert rank_mod.mean_reciprocal_rank(ranks) == pytest.approx(0.125)
