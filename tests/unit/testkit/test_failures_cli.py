"""CLI, fingerprint, and open/mark behavior. The catalog Expect line arrives in P0B-06."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testkit.failure_ledger import LedgerWriter
from testkit.failures.__main__ import main, render_prompt
from testkit.failures.store import (
    area_of,
    fingerprint,
    normalize_message,
    prune,
    tier_from_nodeid,
)

_COUNTS_FAIL = {
    "passed": 0,
    "failed": 1,
    "errors": 0,
    "skipped": 0,
    "xfailed": 0,
    "xpassed_strict": 0,
}
_COUNTS_PASS = {
    "passed": 1,
    "failed": 0,
    "errors": 0,
    "skipped": 0,
    "xfailed": 0,
    "xpassed_strict": 0,
}


def _fail(
    writer: LedgerWriter,
    nodeid: str,
    *,
    exc_type: str = "AssertionError",
    message: str = "boom",
    case: str | None = None,
) -> dict[str, object]:
    return writer.record_failure(
        nodeid=nodeid,
        path=nodeid.split("::", 1)[0],
        phase="call",
        outcome="failed",
        exc_type=exc_type,
        message=message,
        case=case,
        tier="t1",
    )


def test_fingerprint_ignores_line_numbers_and_temp_paths() -> None:
    frame = "tests/unit/testkit/test_failures_cli.py:test_fingerprint_ignores_line_numbers_and_temp_paths"
    first = fingerprint(
        "tests/unit/test_a.py::test_one",
        "AssertionError",
        "bad at line 12 path /tmp/pytest-of-user/abc12345 extra",
        frame,
    )
    second = fingerprint(
        "tests/unit/test_a.py::test_one",
        "AssertionError",
        "bad at line 99 path /private/tmp/pytest-of-user/deadbeef99 extra",
        frame,
    )
    other = fingerprint(
        "tests/unit/test_a.py::test_one",
        "RuntimeError",
        "bad at line 12 path /tmp/pytest-of-user/abc12345 extra",
        frame,
    )
    assert first == second
    assert first != other
    assert normalize_message("line 12 /tmp/pytest-of-x/abc") == normalize_message(
        "line 99 /tmp/pytest-of-y/def45678"
    )


def test_tier_and_area_from_ids() -> None:
    assert tier_from_nodeid("tests/unit/core/test_loop.py::test_a") == "t1"
    assert tier_from_nodeid("regression/selftest/test_plugin.py::test_a") == "t1"
    assert tier_from_nodeid("regression/t2/loop/test_a.py::test_a") == "t2"
    assert tier_from_nodeid("regression/t4/smoke/test_a.py::test_a") == "t4"
    assert tier_from_nodeid("test_local.py::test_a") is None
    assert area_of("T2-LOOP-07") == "LOOP"
    assert area_of("T4-CODE-04") == "CODE"
    assert area_of(None) is None


def test_open_closes_when_nodeid_passes_later(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nodeid = "tests/unit/test_a.py::test_one"
    first = LedgerWriter(tmp_path)
    first.start(argv=["pytest"], markexpr=None, workers=1)
    failed = _fail(first, nodeid, message="still broken")
    first.finish(exit_status=1, counts=_COUNTS_FAIL, outcomes=[(nodeid, "failed")])
    fp = str(failed["fingerprint"])
    assert main(["--ledger-dir", str(tmp_path), "open"]) == 0
    assert fp in capsys.readouterr().out

    second = LedgerWriter(tmp_path)
    second.start(argv=["pytest"], markexpr=None, workers=1)
    second.finish(exit_status=0, counts=_COUNTS_PASS, outcomes=[(nodeid, "passed")])
    assert main(["--ledger-dir", str(tmp_path), "open"]) == 0
    closed = capsys.readouterr().out
    assert "no open failures" in closed
    assert fp not in closed


def test_failure_reopens_after_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nodeid = "tests/unit/test_a.py::test_one"
    first = LedgerWriter(tmp_path)
    first.start(argv=["pytest"], markexpr=None, workers=1)
    _fail(first, nodeid)
    first.finish(exit_status=1, counts=_COUNTS_FAIL, outcomes=[(nodeid, "failed")])

    second = LedgerWriter(tmp_path)
    second.start(argv=["pytest"], markexpr=None, workers=1)
    second.finish(exit_status=0, counts=_COUNTS_PASS, outcomes=[(nodeid, "passed")])

    third = LedgerWriter(tmp_path)
    third.start(argv=["pytest"], markexpr=None, workers=1)
    again = _fail(third, nodeid)
    third.finish(exit_status=1, counts=_COUNTS_FAIL, outcomes=[(nodeid, "failed")])

    assert main(["--ledger-dir", str(tmp_path), "open"]) == 0
    text = capsys.readouterr().out
    assert str(again["fingerprint"]) in text
    assert "count=2" in text


def test_mark_round_trip_and_prompt_omits_expect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    writer = LedgerWriter(tmp_path)
    writer.start(argv=["pytest", "tests"], markexpr=None, workers=1)
    failed = _fail(
        writer,
        "tests/unit/test_a.py::test_one",
        message="needs a fix",
        case="T1-GW-01",
    )
    writer.finish(
        exit_status=1,
        counts={
            "passed": 0,
            "failed": 1,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed_strict": 0,
        },
        outcomes=[("tests/unit/test_a.py::test_one", "failed")],
    )
    fp = str(failed["fingerprint"])
    assert (
        main(
            [
                "--ledger-dir",
                str(tmp_path),
                "mark",
                fp,
                "--status",
                "investigating",
                "--note",
                "looking",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--ledger-dir", str(tmp_path), "open"]) == 0
    opened = capsys.readouterr().out
    assert fp in opened
    assert "status=investigating" in opened

    assert main(["--ledger-dir", str(tmp_path), "prompt", fp]) == 0
    brief = capsys.readouterr().out
    assert "Fix the code under test, not the assertion" in brief
    assert "T1-GW-01" in brief
    assert "needs a fix" in brief
    assert not any(line.startswith("Expect") for line in brief.splitlines())
    assert render_prompt(failed) == brief

    assert main(["--ledger-dir", str(tmp_path), "show", f"{writer.run_id}:1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["fingerprint"] == fp
    assert shown["catalog_expect"] is None

    assert main(["--ledger-dir", str(tmp_path), "export", "--format", "json"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported[0]["fingerprint"] == fp
    assert exported[0]["status"] == "investigating"

    assert main(["--ledger-dir", str(tmp_path), "latest"]) == 0
    latest = capsys.readouterr().out
    assert writer.run_id in latest
    assert "tests/unit/test_a.py::test_one" in latest


def test_killed_writer_is_marked_incomplete(tmp_path: Path) -> None:
    first = LedgerWriter(tmp_path)
    first.start(argv=["pytest"], markexpr=None, workers=1)
    _fail(first, "tests/unit/test_a.py::test_one")
    assert first.path is not None
    text = first.path.read_text(encoding="utf-8")
    assert '"kind": "failure"' in text
    assert '"kind": "run_end"' not in text

    second = LedgerWriter(tmp_path)
    second.start(argv=["pytest"], markexpr=None, workers=1)
    second.finish(
        exit_status=0,
        counts={
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed_strict": 0,
        },
        outcomes=[],
    )
    marked = first.path.read_text(encoding="utf-8")
    assert '"complete": false' in marked
    assert '"kind": "failure"' in marked


def test_prune_keeps_recent_runs_and_ignores_subfolders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_TEST_LEDGER_KEEP", "1")
    monkeypatch.setenv("MONKEYBOT_TEST_LEDGER_DAYS", "30")
    old = tmp_path / "20200101T000000Z-abcdef0-1.jsonl"
    recent = tmp_path / "20261020T000000Z-abcdef0-2.jsonl"
    newest = tmp_path / "20261025T000000Z-abcdef0-3.jsonl"
    for path in (old, recent, newest):
        path.write_text("{}\n", encoding="utf-8")
        path.with_name(path.name.replace(".jsonl", ".outcomes.tsv.gz")).write_bytes(b"")
    (tmp_path / "index.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "triage.json").write_text("{}", encoding="utf-8")
    (tmp_path / "phase0a").mkdir()
    (tmp_path / "phase0a" / "before_ids.txt").write_text("x", encoding="utf-8")

    removed = prune(tmp_path)
    assert old.name in removed
    assert not old.exists()
    assert not old.with_name(old.name.replace(".jsonl", ".outcomes.tsv.gz")).exists()
    assert recent.exists()
    assert newest.exists()
    assert (tmp_path / "index.jsonl").exists()
    assert (tmp_path / "triage.json").exists()
    assert (tmp_path / "phase0a" / "before_ids.txt").read_text(encoding="utf-8") == "x"

    assert main(["--ledger-dir", str(tmp_path), "prune"]) == 0


def test_group_latest_lists_every_run_in_the_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_TEST_RUN_GROUP", "gate-1")
    first = LedgerWriter(tmp_path)
    first.start(argv=["pytest", "tests"], markexpr="t1", workers=2)
    _fail(first, "tests/unit/test_a.py::test_one", message="a")
    first.finish(
        exit_status=1,
        counts={
            "passed": 0,
            "failed": 1,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed_strict": 0,
        },
        outcomes=[("tests/unit/test_a.py::test_one", "failed")],
    )
    second = LedgerWriter(tmp_path)
    second.start(argv=["pytest", "cli"], markexpr=None, workers=1)
    _fail(second, "cli/tests/test_b.py::test_two", message="b")
    second.finish(
        exit_status=1,
        counts={
            "passed": 0,
            "failed": 1,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed_strict": 0,
        },
        outcomes=[("cli/tests/test_b.py::test_two", "failed")],
    )
    assert main(["--ledger-dir", str(tmp_path), "latest", "--group", "gate-1"]) == 0
    text = capsys.readouterr().out
    assert "test_one" in text
    assert "test_two" in text
