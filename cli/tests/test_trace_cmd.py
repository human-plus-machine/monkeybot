"""CLI coverage for `monkeybot trace list` and `digest`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot_cli.main import main


def _session(tmp_path: Path) -> Path:
    session = tmp_path / "workspace" / ".monkeybot" / "transcripts" / "20260101T000000Z_sess-1"
    session.mkdir(parents=True)
    records = [
        {"type": "SessionManifest", "session_id": "sess-1", "model": "m"},
        {"seq": 1, "type": "UserMessage", "content": "hello"},
        {
            "seq": 2,
            "type": "HarnessIntervention",
            "intervention": "doom_loop",
            "detail": "stuck",
            "inner_turn": 1,
        },
        {"seq": 3, "type": "ProviderResponse", "text": "recovered", "tool_requests": []},
    ]
    (session / "transcript.ndjson").write_text(
        "".join(json.dumps(row) + "\n" for row in records),
        encoding="utf-8",
    )
    return session


def test_trace_list_digest_and_seq(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _session(tmp_path)
    assert main(["trace", "list", "--cwd", str(tmp_path)]) == 0
    listed = capsys.readouterr().out
    assert "sess-1" in listed
    assert "interventions=1" in listed
    assert "score=1" in listed

    assert main(["trace", "digest", "sess-1", "--cwd", str(tmp_path)]) == 0
    digest = capsys.readouterr().out
    assert "@seq 2" in digest
    assert "doom_loop" in digest
    assert "recovered" in digest

    assert main(["trace", "digest", "sess-1", "--cwd", str(tmp_path), "--seq", "2"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["intervention"] == "doom_loop"
    assert record["detail"] == "stuck"


def test_trace_digest_missing_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["trace", "digest", "missing", "--cwd", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "No transcript found" in err


def test_trace_read_failure_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _session(tmp_path)
    ndjson = session / "transcript.ndjson"
    ndjson.chmod(0)
    try:
        assert main(["trace", "digest", "sess-1", "--cwd", str(tmp_path)]) == 1
        assert "Transcript read failed" in capsys.readouterr().err
        assert main(["trace", "list", "--cwd", str(tmp_path)]) == 1
        assert "Transcript read failed" in capsys.readouterr().err
    finally:
        ndjson.chmod(0o644)
