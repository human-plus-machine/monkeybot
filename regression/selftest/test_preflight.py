"""Self-checks for the live preflight (regression/t4/preflight.py). No network, no real key.

Every provider here is a local stand-in; the key files are temporary and hold made-up values.
The real repo-root .env is never read: ``ENV_FILE`` points into ``tmp_path`` in every test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.llm.provider import Done, TextDelta
from regression.t4 import preflight
from regression.t4.preflight import (
    KEY_VAR,
    MISSING_KEY_MESSAGE,
    PreflightError,
    ensure_key,
    run_preflight,
    run_preflight_once,
)
from regression.t4.report import main

FAKE_KEY = "fake-key-0123456789abcdef"  # not a real key; never leaves this process
_DEFAULT_ENV_FILE = preflight.ENV_FILE  # captured before any test patches it; never opened


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No key in the environment and no key file anywhere the code could look.

    Any spelling of the key name is removed too: deepeval's pytest plugin loads ``<cwd>/.env``
    into the environment at startup, so a lowercase ``ollama_api_key`` can be ambient here.
    """
    import os

    monkeypatch.setenv(KEY_VAR, "placeholder")
    monkeypatch.delenv(KEY_VAR)  # registers the restore; also undoes ensure_key's export
    for name in [n for n in os.environ if n.upper() == KEY_VAR]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("MONKEYBOT_AGENT_CONFIG", raising=False)
    missing = tmp_path / "no-such.env"
    monkeypatch.setattr(preflight, "ENV_FILE", missing)
    preflight._passed.clear()
    return missing


class _Stream:
    """A provider whose ``stream`` yields scripted events or raises."""

    def __init__(self, events: Sequence[Any] = (), *, error: Exception | None = None) -> None:
        self.events = list(events)
        self.error = error
        self.calls = 0
        self.models: list[str] = []

    def stream(
        self, messages: Sequence[Any], tools: Sequence[Any], *, model: str
    ) -> AsyncIterator[Any]:
        self.calls += 1
        self.models.append(model)
        return self._run()

    async def _run(self) -> AsyncIterator[Any]:
        if self.error is not None:
            raise self.error
        for event in self.events:
            yield event


class _Hang(_Stream):
    async def _run(self) -> AsyncIterator[Any]:
        await asyncio.sleep(30)
        yield Done()


def _records(ledger_dir: Path) -> list[dict[str, Any]]:
    files = sorted(ledger_dir.rglob("*.jsonl"))
    files = [f for f in files if f.name != "index.jsonl"]
    assert len(files) == 1, files
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def test_missing_key_fails_closed_before_any_provider_call(tmp_path: Path) -> None:
    provider = _Stream([Done()])
    with pytest.raises(PreflightError) as caught:
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            env_file=tmp_path / "absent.env",
            provider_factory=lambda: provider,
        )
    assert str(caught.value).splitlines()[0] == MISSING_KEY_MESSAGE
    assert MISSING_KEY_MESSAGE == (
        "OLLAMA_API_KEY is not set (set it in the environment or in the repo-root .env)"
    )
    assert caught.value.reason == "missing_key"
    assert provider.calls == 0

    start, error, end = _records(tmp_path / "ledger")
    assert (start["kind"], error["kind"], end["kind"]) == ("run_start", "run_error", "run_end")
    assert error["reason"] == "missing_key"
    assert error["message"] == MISSING_KEY_MESSAGE
    assert error["tier"] == "t4"
    assert error["live"] == {
        "provider": "ollama-cloud",
        "model": "glm-5.3-flash",
        "agent_url": "http://127.0.0.1:8787",
        "suite": "smoke",
    }
    assert end["exit_status"] == 2 and end["complete"] is True and end["counts"]["errors"] == 1


def test_a_blank_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(KEY_VAR, "   ")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY_VAR}=\n", encoding="utf-8")
    assert ensure_key(env_file) is False


def test_a_key_in_the_env_file_is_exported_for_this_process(tmp_path: Path) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY_VAR}={FAKE_KEY}\nOTHER=1\n", encoding="utf-8")
    assert ensure_key(env_file) is True
    assert os.environ[KEY_VAR] == FAKE_KEY
    assert "OTHER" not in os.environ


def test_the_environment_wins_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    monkeypatch.setenv(KEY_VAR, "from-the-environment-01")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY_VAR}={FAKE_KEY}\n", encoding="utf-8")
    assert ensure_key(env_file) is True
    assert os.environ[KEY_VAR] == "from-the-environment-01"


def test_the_default_key_file_is_the_repo_root_env() -> None:
    default = _DEFAULT_ENV_FILE
    assert default == Path(__file__).resolve().parents[2] / ".env"
    assert default != preflight.AGENT_ROOT / ".env"


def test_a_lowercase_key_name_in_the_root_env_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    root_env = tmp_path / ".env"
    root_env.write_text(f"ollama_api_key={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "ENV_FILE", root_env)
    provider = _Stream([Done()])
    run_preflight(
        suite="smoke",
        agent_url="http://127.0.0.1:8787",
        ledger_dir=tmp_path / "ledger",
        provider_factory=lambda: provider,
    )
    assert provider.calls == 1
    assert os.environ[KEY_VAR] == FAKE_KEY
    assert "ollama_api_key" not in os.environ
    assert not (tmp_path / "ledger").exists()


def test_the_environment_wins_over_a_lowercase_key_in_the_root_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    monkeypatch.setenv(KEY_VAR, "from-the-environment-02")
    root_env = tmp_path / ".env"
    root_env.write_text(f"ollama_api_key={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "ENV_FILE", root_env)
    assert ensure_key() is True
    assert os.environ[KEY_VAR] == "from-the-environment-02"


def test_an_exact_case_key_line_wins_over_other_spellings(tmp_path: Path) -> None:
    import os

    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ollama_api_key=lowercase-value-03\n{KEY_VAR}={FAKE_KEY}\n", encoding="utf-8"
    )
    assert ensure_key(env_file) is True
    assert os.environ[KEY_VAR] == FAKE_KEY


def test_a_root_env_without_the_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root_env = tmp_path / ".env"
    root_env.write_text(
        "OTHER_API_KEY=x\nollama_api_key_old=stale-value-04\nollama_api_key=\n", encoding="utf-8"
    )
    monkeypatch.setattr(preflight, "ENV_FILE", root_env)
    provider = _Stream([Done()])
    with pytest.raises(PreflightError) as caught:
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            provider_factory=lambda: provider,
        )
    assert caught.value.reason == "missing_key"
    assert str(caught.value).splitlines()[0] == MISSING_KEY_MESSAGE
    assert provider.calls == 0
    assert _records(tmp_path / "ledger")[1]["reason"] == "missing_key"


def test_a_working_stream_passes_and_writes_no_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)
    provider = _Stream([TextDelta(text="ok"), Done()])
    run_preflight(
        suite="smoke",
        agent_url="http://127.0.0.1:8787",
        ledger_dir=tmp_path / "ledger",
        provider_factory=lambda: provider,
    )
    assert provider.calls == 1
    assert provider.models == ["glm-5.3-flash"]
    assert not (tmp_path / "ledger").exists()


def test_a_rejected_key_or_model_fails_fast_with_the_provider_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)
    provider = _Stream(error=RuntimeError(f"401 Unauthorized for key {FAKE_KEY}: invalid"))
    with pytest.raises(PreflightError) as caught:
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            provider_factory=lambda: provider,
        )
    text = str(caught.value)
    assert caught.value.reason == "stream_failed"
    assert "401 Unauthorized" in text
    assert FAKE_KEY not in text

    _, error, _ = _records(tmp_path / "ledger")
    assert error["reason"] == "stream_failed"
    assert "401 Unauthorized" in error["message"]
    assert FAKE_KEY not in json.dumps(_records(tmp_path / "ledger"))


def test_a_stream_without_done_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)
    with pytest.raises(PreflightError, match="without a Done event"):
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            provider_factory=lambda: _Stream([TextDelta(text="ok")]),
        )


def test_a_hung_stream_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)
    with pytest.raises(PreflightError, match="TimeoutError"):
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            provider_factory=lambda: _Hang(),
            timeout_sec=0.05,
        )


def test_ledger_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(PreflightError) as caught:
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            ledger=False,
            env_file=tmp_path / "absent.env",
        )
    assert str(caught.value) == MISSING_KEY_MESSAGE
    assert not (tmp_path / "ledger").exists()


def test_the_agent_config_must_name_ollama_cloud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)
    config = tmp_path / "monkeybot.yaml"
    config.write_text("model:\n  provider: gemini\n  name: gemini-2.5-flash\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_AGENT_CONFIG", str(config))
    with pytest.raises(PreflightError, match="live runs use ollama-cloud") as caught:
        run_preflight(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger=False,
        )
    assert caught.value.reason == "unsupported_provider"


def test_once_skips_a_check_that_already_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)
    provider = _Stream([Done()])
    for _ in range(3):
        run_preflight_once(
            suite="smoke",
            agent_url="http://127.0.0.1:8787",
            ledger_dir=tmp_path / "ledger",
            provider_factory=lambda: provider,
        )
    assert provider.calls == 1


def test_the_agent_model_comes_from_the_agent_project() -> None:
    assert preflight.agent_model() == ("ollama-cloud", "glm-5.3-flash")


def test_the_cli_exits_2_with_the_message_and_a_run_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # main() sets this default for a live run; the monkeypatch restores it afterwards.
    monkeypatch.setenv("MONKEYBOT_AGENT_CONFIG", str(preflight.AGENT_CONFIG))
    code = main(["--suite", "smoke", "--ledger-dir", str(tmp_path / "ledger")])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.splitlines()[0] == MISSING_KEY_MESSAGE
    assert "run_error recorded to" in captured.err
    assert captured.out == ""
    assert _records(tmp_path / "ledger")[1]["kind"] == "run_error"


def test_the_cli_no_ledger_flag_leaves_no_record(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MONKEYBOT_AGENT_CONFIG", str(preflight.AGENT_CONFIG))
    code = main(["--suite", "smoke", "--no-ledger", "--ledger-dir", str(tmp_path / "ledger")])
    assert code == 2
    assert capsys.readouterr().err.strip() == MISSING_KEY_MESSAGE
    assert not (tmp_path / "ledger").exists()
