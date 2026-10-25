"""Plugin behavior from section 5.9.6. Inner pytest runs are subprocesses."""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from testkit.failures.store import list_run_files, read_jsonl, run_failures

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def inner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    pytester.makeini(f"[pytest]\npythonpath = {REPO}\naddopts = -p testkit.failure_ledger\n")
    original = pytester.makepyfile

    def dedented(*args: str, **kwargs: str) -> object:
        clean_args = tuple(textwrap.dedent(arg).strip() + "\n" for arg in args)
        clean_kwargs = {key: textwrap.dedent(value).strip() + "\n" for key, value in kwargs.items()}
        return original(*clean_args, **clean_kwargs)

    pytester.makepyfile = dedented  # type: ignore[method-assign]
    ledger = pytester.path / "ledger"
    ledger.mkdir()
    return ledger


def _records(ledger: Path) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in list_run_files(ledger):
        found.extend(run_failures(read_jsonl(path)))
    return found


def _footer(ledger: Path) -> dict[str, object]:
    files = list_run_files(ledger)
    assert files, "expected a run file"
    records = read_jsonl(files[-1])
    footer = next(record for record in reversed(records) if record.get("kind") == "run_end")
    return footer


def test_failure_error_and_collection_each_record_once(
    pytester: pytest.Pytester, inner: Path
) -> None:
    pytester.makepyfile(
        test_outcomes="""
        import pytest

        def test_pass():
            assert True

        def test_fail():
            raise AssertionError("boom")

        def test_skip():
            pytest.skip("nope")

        @pytest.fixture
        def broken():
            raise RuntimeError("setup broke")

        def test_err(broken):
            assert True
        """,
    )
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner))
    assert int(result.ret) == 1
    records = _records(inner)
    phases = sorted((str(record["phase"]), str(record["outcome"])) for record in records)
    assert phases == [("call", "failed"), ("setup", "failed")]
    assert all(record["nodeid"] for record in records)
    failed = next(record for record in records if record["phase"] == "call")
    assert failed["exc_type"] == "AssertionError"
    assert failed["message"] == "boom"
    setup = next(record for record in records if record["phase"] == "setup")
    assert setup["exc_type"] == "RuntimeError"
    assert "setup broke" in str(setup["message"])
    counts = _footer(inner)["counts"]
    assert isinstance(counts, dict)
    assert counts["passed"] == 1
    assert counts["failed"] == 1
    assert counts["errors"] == 1
    assert counts["skipped"] == 1


def test_collection_error_records_once(pytester: pytest.Pytester, inner: Path) -> None:
    pytester.makepyfile(
        test_collect="""
        raise RuntimeError("collect broke")
        """
    )
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner))
    assert int(result.ret) != 0
    records = _records(inner)
    assert len(records) == 1
    assert records[0]["phase"] == "collect"
    assert records[0]["outcome"] == "failed"
    assert records[0]["exc_type"] == "RuntimeError"
    assert "collect broke" in str(records[0]["message"])


def test_xdist_records_are_unique_and_contiguous(pytester: pytest.Pytester, inner: Path) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parametrize("n", [1, 2, 3, 4])
        def test_fail(n):
            raise AssertionError(f"bad {n}")
        """
    )
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner), "-n", "4")
    assert int(result.ret) == 1
    records = _records(inner)
    seqs = [int(record["seq"]) for record in records]
    nodeids = [str(record["nodeid"]) for record in records]
    assert seqs == [1, 2, 3, 4]
    assert len(set(nodeids)) == 4
    assert all(record["worker"] for record in records)


def test_xdist_forwards_one_collection_error(pytester: pytest.Pytester, inner: Path) -> None:
    pytester.makepyfile(
        test_ok="""
        def test_ok():
            assert True
        """,
        test_bad="""
        raise RuntimeError("collect broke")
        """,
    )
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner), "-n", "4")
    assert int(result.ret) != 0
    collected = [record for record in _records(inner) if record["phase"] == "collect"]
    assert len(collected) == 1
    assert collected[0]["outcome"] == "failed"
    assert "collect broke" in str(collected[0]["message"])


def test_exit_code_matches_without_ledger(pytester: pytest.Pytester, inner: Path) -> None:
    green = pytester.path / "green"
    red = pytester.path / "red"
    interrupted = pytester.path / "interrupted"
    for path in (green, red, interrupted):
        path.mkdir()
    pytester.makepyfile(
        test_green="""
        def test_ok():
            assert True
        """
    )
    on = pytester.runpytest_subprocess("--ledger-dir", str(green), "test_green.py")
    off = pytester.runpytest_subprocess("--no-ledger", "--ledger-dir", str(green), "test_green.py")
    assert int(on.ret) == int(off.ret) == 0
    assert "monkeybot failure ledger" not in "\n".join(on.outlines)

    pytester.makepyfile(
        test_red="""
        def test_bad():
            raise AssertionError("nope")
        """
    )
    on_red = pytester.runpytest_subprocess("--ledger-dir", str(red), "test_red.py")
    off_red = pytester.runpytest_subprocess("--no-ledger", "--ledger-dir", str(red), "test_red.py")
    assert int(on_red.ret) == int(off_red.ret) != 0
    assert "monkeybot failure ledger" in "\n".join(on_red.outlines)
    assert "monkeybot failure ledger" not in "\n".join(off_red.outlines)
    assert list_run_files(red)

    pytester.makepyfile(
        test_interrupt="""
        def test_die():
            raise KeyboardInterrupt()
        """
    )
    on_stop = pytester.runpytest_subprocess("--ledger-dir", str(interrupted), "test_interrupt.py")
    off_stop = pytester.runpytest_subprocess(
        "--no-ledger", "--ledger-dir", str(interrupted), "test_interrupt.py"
    )
    assert int(on_stop.ret) == int(off_stop.ret) != 0


def test_killed_run_is_marked_incomplete_by_the_next_run(
    pytester: pytest.Pytester, inner: Path
) -> None:
    target = pytester.path / "test_kill.py"
    target.write_text(
        "def test_fail():\n"
        "    raise AssertionError('boom')\n"
        "def test_hang():\n"
        "    import time\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    proc = pytester.popen(
        [
            sys.executable,
            "-mpytest",
            "--ledger-dir",
            str(inner),
            str(target),
        ]
    )
    deadline = time.time() + 20
    run_file: Path | None = None
    while time.time() < deadline:
        files = list_run_files(inner)
        if files and '"kind": "failure"' in files[0].read_text(encoding="utf-8"):
            run_file = files[0]
            break
        time.sleep(0.05)
    assert run_file is not None, "failure line was not flushed before timeout"
    proc.kill()
    proc.wait(timeout=10)
    assert '"kind": "run_end"' not in run_file.read_text(encoding="utf-8")

    target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner), str(target))
    assert int(result.ret) == 0
    records = read_jsonl(run_file)
    assert any(record.get("kind") == "failure" for record in records)
    footer = next(record for record in records if record.get("kind") == "run_end")
    assert footer["complete"] is False


def test_secrets_in_message_stdout_and_traceback_are_scrubbed(
    pytester: pytest.Pytester, inner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret-value-123")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "unit-gcp-project")
    monkeypatch.setenv("UNIT_API_KEY", "1")
    monkeypatch.setenv("UNIT_TOKEN", "true")
    pytester.makepyfile(
        """
        def test_leak():
            api_key = "sk-tracebacksecretvalue123456"
            print("Authorization: Bearer sk-stdoutsecretvalue123456")
            print("x-api-key: headersecretvalue123456")
            print("n=1 flag true")
            raise AssertionError(
                "leaked nvapi-assertsecretvalue123456 "
                "ollama-secret-value-123 unit-gcp-project n=1 flag true"
            )
        """
    )
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner), "-l", "--tb=long")
    assert int(result.ret) == 1
    blob = "\n".join(path.read_text(encoding="utf-8") for path in list_run_files(inner))
    for secret in (
        "sk-tracebacksecretvalue123456",
        "sk-stdoutsecretvalue123456",
        "headersecretvalue123456",
        "nvapi-assertsecretvalue123456",
        "ollama-secret-value-123",
        "unit-gcp-project",
    ):
        assert secret not in blob
    records = _records(inner)
    assert records
    assert "n=1 flag true" in str(records[0]["message"])
    assert "sk-tracebacksecretvalue123456" not in str(records[0]["traceback"])
    captured = records[0]["captured"]
    assert isinstance(captured, dict)
    assert "sk-stdoutsecretvalue123456" not in str(captured.get("stdout"))
    assert "headersecretvalue123456" not in str(captured.get("stdout"))


def test_unwritable_ledger_warns_once_and_keeps_the_exit_code(
    pytester: pytest.Pytester, inner: Path
) -> None:
    blocker = pytester.path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    pytester.makepyfile(
        """
        def test_bad():
            raise AssertionError("boom")
        """
    )
    warned = pytester.runpytest_subprocess("--ledger-dir", str(blocker / "ledger"))
    silent = pytester.runpytest_subprocess("--no-ledger", "--ledger-dir", str(inner))
    assert int(warned.ret) == int(silent.ret) != 0
    err = "\n".join(warned.errlines)
    assert err.count("monkeybot failure ledger warning") == 1


def test_banner_only_when_failures_were_recorded(pytester: pytest.Pytester, inner: Path) -> None:
    pytester.makepyfile(
        test_bad="""
        def test_bad():
            raise AssertionError("boom")
        """
    )
    failed = pytester.runpytest_subprocess("--ledger-dir", str(inner), "test_bad.py")
    text = "\n".join(failed.outlines)
    assert "monkeybot failure ledger" in text
    assert "do not change code or" in text
    assert "uv run python -m testkit.failures latest" in text

    green = inner.parent / "green-ledger"
    green.mkdir()
    pytester.makepyfile(
        test_ok="""
        def test_ok():
            assert True
        """
    )
    passed = pytester.runpytest_subprocess("--ledger-dir", str(green), "test_ok.py")
    assert int(passed.ret) == 0
    assert "monkeybot failure ledger" not in "\n".join(passed.outlines)


def test_case_marker_and_path_tier(pytester: pytest.Pytester, inner: Path) -> None:
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.t2
        @pytest.mark.case("T2-LOOP-07")
        def test_marked():
            raise AssertionError("marked")
        """
    )
    path_dir = pytester.path / "tests"
    path_dir.mkdir()
    (path_dir / "test_path.py").write_text(
        "def test_path():\n    raise AssertionError('path')\n",
        encoding="utf-8",
    )
    result = pytester.runpytest_subprocess("--ledger-dir", str(inner))
    assert int(result.ret) == 1
    records = _records(inner)
    by_node = {str(record["nodeid"]): record for record in records}
    marked = next(record for nodeid, record in by_node.items() if nodeid.endswith("test_marked"))
    assert marked["tier"] == "t2"
    assert marked["case"] == "T2-LOOP-07"
    assert marked["area"] == "LOOP"
    assert marked["catalog_expect"] is None
    pathed = next(record for nodeid, record in by_node.items() if "test_path.py" in nodeid)
    assert pathed["tier"] == "t1"
    assert pathed["case"] is None


def test_run_file_is_valid_jsonl(pytester: pytest.Pytester, inner: Path) -> None:
    pytester.makepyfile(
        """
        def test_bad():
            raise AssertionError("boom")
        """
    )
    pytester.runpytest_subprocess("--ledger-dir", str(inner))
    files = list_run_files(inner)
    assert len(files) == 1
    kinds = []
    for line in files[0].read_text(encoding="utf-8").splitlines():
        kinds.append(json.loads(line)["kind"])
    assert kinds[0] == "run_start"
    assert "failure" in kinds
    assert kinds[-1] == "run_end"
    latest = json.loads((inner / "LATEST.json").read_text(encoding="utf-8"))
    assert latest["complete"] is True
    assert latest["run_id"]
    assert (inner / "index.jsonl").is_file()
    assert files[0].with_name(files[0].name.replace(".jsonl", ".outcomes.tsv.gz")).is_file()
